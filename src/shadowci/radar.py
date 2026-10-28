"""Phase 5: structured state for the Bug-Radar dashboard.

`shadowci watch` drives the pipeline (`pipeline.py`) and prints everything
as it happens — fine for demos and CI, but not an ambient view a developer
would leave open. `RadarEngine` drives the exact same pipeline but returns
a `RadarState` snapshot from each poll cycle instead of printing, so a
terminal dashboard (`tui.py`) can render watched files, in-flight change
sets, live check status, and pending ghost patches with their
interruption-policy verdict. Accept/reject/ignore keypresses route through
`pipeline.accept_patch` / `reject_patch` / `ignore_patch` — the same
functions `shadowci apply-ghost` / `reject-ghost` / `ignore-ghost` call —
so a patch handled from the dashboard and one handled from the plain CLI
are indistinguishable afterwards.

`render_lines` has no curses dependency so it can be unit tested as plain
text; `tui.py` is the thin curses loop built on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from shadowci import pipeline
from shadowci.changeset import ChangeSetDetector
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeSet, CheckResult, FileChangeEvent
from shadowci.interruption import InterruptionDecision, InterruptionPolicy
from shadowci.providers import create_provider
from shadowci.shadow_worktree import ShadowWorktree
from shadowci.watcher import PollingFileWatcher

MAX_LOG_LINES = 200
MAX_RECENT_EVENTS = 20


@dataclass(frozen=True)
class PendingGhostPatch:
    """A saved ghost patch still on disk, with its current policy verdict."""

    path: Path
    digest: str
    fingerprint: str
    summary: str
    decision: InterruptionDecision


@dataclass(frozen=True)
class RadarState:
    """One snapshot of the whole watch-check-escalate-verify pipeline."""

    project_root: Path
    provider: str
    cycle: int = 0
    pending_changesets: int = 0
    recent_events: tuple[FileChangeEvent, ...] = ()
    last_changeset: ChangeSet | None = None
    last_check_results: tuple[CheckResult, ...] = ()
    pending_patches: tuple[PendingGhostPatch, ...] = ()
    log_lines: tuple[str, ...] = ()


def scan_pending_patches(root: Path, config: ShadowCIConfig) -> tuple[PendingGhostPatch, ...]:
    """Every saved ghost patch still on disk, with a fresh policy verdict.

    Rescanned from disk every cycle rather than cached in memory, so a
    patch applied/rejected/ignored elsewhere (another terminal running the
    plain `shadowci` CLI commands) is reflected the moment the dashboard
    next renders, with no shared state to keep in sync by hand.
    """
    ghost_dir = pipeline.ghost_patch_dir(root)
    store = pipeline.signal_store(root)
    policy = InterruptionPolicy(
        store,
        threshold=config.interruption_threshold,
        min_signals=config.interruption_min_signals,
    )

    patches = []
    for patch_path in sorted(ghost_dir.glob("*.patch")):
        metadata = pipeline.load_patch_metadata(patch_path)
        if metadata is None:
            continue
        decision = policy.evaluate_fingerprint(metadata["fingerprint"])
        patches.append(
            PendingGhostPatch(
                path=patch_path,
                digest=metadata["digest"],
                fingerprint=metadata["fingerprint"],
                summary=metadata["summary"],
                decision=decision,
            )
        )
    return tuple(patches)


class RadarEngine:
    """Drives one poll cycle of the ShadowCI pipeline, state instead of print."""

    def __init__(self, config: ShadowCIConfig) -> None:
        self._config = config
        self._watcher = PollingFileWatcher(config)
        self._detector = ChangeSetDetector(config)
        self._worktree = ShadowWorktree(config)
        self._state = RadarState(project_root=config.project_root, provider=config.provider)

    @property
    def state(self) -> RadarState:
        return self._state

    @property
    def config(self) -> ShadowCIConfig:
        return self._config

    def ensure_worktree(self) -> Path:
        return self._worktree.ensure_created()

    def note(self, message: str) -> RadarState:
        """Append a log entry without running a poll cycle — for instant
        feedback right after a keybinding triggers an accept/reject/ignore.
        A multi-line `pipeline.ActionResult.message` is split so every
        physical line gets its own entry, same as every other log line."""
        self._state = replace(self._state, log_lines=self._append_log(*message.splitlines()))
        return self._state

    def refresh_patches(self) -> RadarState:
        """Rescan the ghost patch directory without running a poll cycle."""
        patches = scan_pending_patches(self._config.project_root, self._config)
        self._state = replace(self._state, pending_patches=patches)
        return self._state

    def step(self, skip_checks: bool = False) -> RadarState:
        state = self._state
        new_log: tuple[str, ...] = ()

        events = self._watcher.poll()
        if events:
            self._detector.add_events(events)
            new_log = (*new_log, *(f"[{event.kind.value}] {event.path.name}" for event in events))

        changeset = self._detector.poll()
        last_changeset = state.last_changeset
        last_check_results = state.last_check_results

        if changeset is not None:
            touched = self._worktree.sync(list(changeset.events))
            last_changeset = changeset
            new_log = (
                *new_log,
                f"changeset ready: {len(changeset.paths)} file(s) -> synced {len(touched)}",
            )
            if not skip_checks:
                outcome = pipeline.run_checks_and_maybe_escalate(
                    changeset, self._worktree, self._config, create_provider
                )
                last_check_results = outcome.check_results
                new_log = (*new_log, *self._describe_outcome(outcome))

        recent_events = (*state.recent_events, *events)[-MAX_RECENT_EVENTS:]

        self._state = RadarState(
            project_root=state.project_root,
            provider=state.provider,
            cycle=state.cycle + 1,
            pending_changesets=self._detector.pending_count,
            recent_events=recent_events,
            last_changeset=last_changeset,
            last_check_results=last_check_results,
            pending_patches=scan_pending_patches(self._config.project_root, self._config),
            log_lines=self._append_log(*new_log),
        )
        return self._state

    @staticmethod
    def _describe_outcome(outcome: pipeline.PipelineOutcome) -> tuple[str, ...]:
        lines = tuple(
            f"check [{result.name}] {'PASS' if result.passed else 'FAIL'}"
            for result in outcome.check_results
        )
        if outcome.unknown_provider_error is not None:
            lines += (f"skipping escalation: {outcome.unknown_provider_error}",)
        elif outcome.failing_checks:
            if outcome.provider_error is not None:
                lines += (f"provider error: {outcome.provider_error}",)
            elif outcome.ghost_patch_error is not None:
                lines += (f"could not verify a ghost patch: {outcome.ghost_patch_error}",)
            elif outcome.ghost_patch is not None:
                lines += (f"ghost patch verified: {outcome.ghost_patch.evidence.summary}",)
        return lines

    def _append_log(self, *lines: str) -> tuple[str, ...]:
        combined = (*self._state.log_lines, *lines)
        return combined[-MAX_LOG_LINES:]


def render_lines(state: RadarState, selected: int = 0) -> list[str]:
    """Render a `RadarState` snapshot to plain text lines — no curses
    dependency, so this is unit-testable without a real terminal."""
    lines: list[str] = []
    lines.append(f"ShadowCI Bug-Radar — {state.project_root}")
    lines.append(f"provider: {state.provider}  |  cycle: {state.cycle}  |  pending change sets: {state.pending_changesets}")
    lines.append("")

    lines.append("-- recent file events " + "-" * 20)
    if state.recent_events:
        for event in state.recent_events[-8:]:
            try:
                shown = event.path.relative_to(state.project_root)
            except ValueError:
                shown = event.path
            lines.append(f"  [{event.kind.value}] {shown}")
    else:
        lines.append("  (none yet)")
    lines.append("")

    lines.append("-- last check results " + "-" * 20)
    if state.last_check_results:
        for result in state.last_check_results:
            status = "PASS" if result.passed else "FAIL"
            lines.append(f"  [{result.name}] {status} ({result.duration_seconds:.2f}s)")
    else:
        lines.append("  (no checks run yet)")
    lines.append("")

    lines.append(f"-- pending ghost patches ({len(state.pending_patches)}) " + "-" * 10)
    if state.pending_patches:
        for index, patch in enumerate(state.pending_patches):
            marker = ">" if index == selected else " "
            verdict = "surface" if patch.decision.interrupt else "silent"
            lines.append(
                f" {marker} {patch.path.name}  [{patch.fingerprint}]  "
                f"verdict={verdict} score={patch.decision.score:.2f}  {patch.summary}"
            )
    else:
        lines.append("  (none pending)")
    lines.append("")

    lines.append("-- log " + "-" * 30)
    if state.log_lines:
        for line in state.log_lines[-10:]:
            lines.append(f"  {line}")
    else:
        lines.append("  (nothing yet)")
    lines.append("")

    lines.append("[a] accept   [r] reject   [i] ignore   [j/k] move selection   [q] quit")
    return lines
