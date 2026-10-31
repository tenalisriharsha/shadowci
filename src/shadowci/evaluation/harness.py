"""Replay scenarios through the real pipeline and score the result.

This is a third driver over `shadowci.pipeline` — alongside `cli.py`'s
`watch` and `radar.py`/`tui.py` — fed a *simulated* clock instead of
wall-clock time so a whole scenario suite replays in milliseconds instead
of waiting out real debounce/idle windows. Nothing about detection is
mocked: a real git repo, a real shadow worktree, and a real
`GhostPatchBuilder` verify a real diff against real file contents, exactly
as `shadowci watch` would.

Two replay modes:

- `run_scenario` / `run_suite` — one pass per scenario, scoring whether the
  pipeline detects an injected bug (and how fast) or stays silent on a
  clean edit, as it would see it for the first time.
- `run_convergence` — replays the *same* bug-and-fix scenario several times
  in one project, simulating an "accept" after every verified fix. This
  exercises `InterruptionPolicy` the way real usage would: silent for the
  first `min_signals` fixes of a kind, then surfacing once their accept
  history clears the threshold.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from shadowci import pipeline
from shadowci.changeset import ChangeSetDetector
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent
from shadowci.evaluation.scenarios import Scenario
from shadowci.interruption import InterruptionDecision
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree

_IDLE_PADDING_SECONDS = 0.01
_DEFAULT_DEBOUNCE_SECONDS = 0.1
_DEFAULT_IDLE_SECONDS = 0.2


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path, initial_files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _run_git(["init", "-q"], cwd=root)
    _run_git(["config", "user.name", "shadowci-eval"], cwd=root)
    _run_git(["config", "user.email", "eval@shadowci.local"], cwd=root)

    for relative, content in initial_files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    _run_git(["add", "-A"], cwd=root)
    _run_git(["commit", "-q", "-m", "initial"], cwd=root)


def _clear_bytecode_cache(root: Path) -> None:
    """Delete every `__pycache__` under `root`.

    `run_convergence` rewrites the same module path with same-length buggy
    and fixed content, over and over, faster than the filesystem's mtime
    resolution can distinguish — without this, a stale compiled `.pyc` from
    a previous iteration can get served for the next one, making checks
    pass or fail against the wrong source.
    """
    for cache_dir in root.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)


def _build_config(root: Path, debounce_seconds: float, idle_seconds: float) -> ShadowCIConfig:
    return ShadowCIConfig(
        project_root=root,
        shadow_dir=Path(".shadowci/shadow"),
        debounce_seconds=debounce_seconds,
        idle_seconds=idle_seconds,
        provider="scripted",
    )


@dataclass(frozen=True)
class ChangeSetOutcome:
    """One promoted change set's pipeline result, with its simulated timing."""

    detected_at: float
    outcome: pipeline.PipelineOutcome


@dataclass(frozen=True)
class ScenarioResult:
    """A scenario's single-pass replay: whether it matched ground truth."""

    scenario: Scenario
    changesets: tuple[ChangeSetOutcome, ...] = ()
    detected: bool = False
    detection_latency_seconds: float | None = None
    action: str | None = None  # "accept", "reject", or None

    @property
    def true_positive(self) -> bool:
        return self.scenario.bug_expected and self.detected

    @property
    def false_negative(self) -> bool:
        return self.scenario.bug_expected and not self.detected

    @property
    def false_interruption(self) -> bool:
        return not self.scenario.bug_expected and self.detected


def run_scenario(
    scenario: Scenario,
    debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    idle_seconds: float = _DEFAULT_IDLE_SECONDS,
) -> ScenarioResult:
    """Replay one scenario's edits through the real pipeline and score it.

    The provider is fixed to the scenario's own `regression_test` /
    `proposed_fix` (when it has one), so a real `GhostPatchBuilder` verifies
    a real diff against real file contents in a real shadow worktree.
    """
    with TemporaryDirectory(prefix="shadowci-eval-") as tmp:
        root = Path(tmp) / "repo"
        _init_repo(root, scenario.session.initial_files)

        config = _build_config(root, debounce_seconds, idle_seconds)
        worktree = ShadowWorktree(config)
        worktree.ensure_created()
        detector = ChangeSetDetector(config)

        tests = [scenario.regression_test] if scenario.regression_test else None
        fixes = [scenario.proposed_fix] if scenario.proposed_fix else None

        def create_provider(_name: str) -> ScriptedProvider:
            return ScriptedProvider(tests=tests, fixes=fixes)

        edits = sorted(scenario.session.edits, key=lambda edit: edit.offset_seconds)
        bug_introduced_at = edits[0].offset_seconds if edits else 0.0

        changesets: list[ChangeSetOutcome] = []
        pending: list[FileChangeEvent] = []
        for index, edit in enumerate(edits):
            live_path = root / edit.relative_path
            if edit.kind is ChangeKind.DELETED:
                live_path.unlink(missing_ok=True)
            else:
                live_path.parent.mkdir(parents=True, exist_ok=True)
                live_path.write_text(edit.content or "")

            pending.append(
                FileChangeEvent(path=live_path, kind=edit.kind, timestamp=edit.offset_seconds)
            )

            is_last = index == len(edits) - 1
            gap_to_next = (
                None if is_last else edits[index + 1].offset_seconds - edit.offset_seconds
            )
            if not is_last and (gap_to_next is None or gap_to_next < idle_seconds):
                continue

            detector.add_events(pending)
            now = edit.offset_seconds + idle_seconds + _IDLE_PADDING_SECONDS
            changeset = detector.poll(now=now)
            pending = []
            if changeset is None:
                continue

            touched = worktree.sync(list(changeset.events))
            if not touched:
                continue

            result = pipeline.run_checks_and_maybe_escalate(
                changeset, worktree, config, create_provider
            )
            changesets.append(ChangeSetOutcome(detected_at=now, outcome=result))

        detected_at = next(
            (cs.detected_at for cs in changesets if cs.outcome.ghost_patch is not None),
            None,
        )

        action = None
        for changeset_outcome in changesets:
            patch_path = changeset_outcome.outcome.ghost_patch_path
            if changeset_outcome.outcome.ghost_patch is None or patch_path is None:
                continue
            if scenario.bug_expected:
                pipeline.accept_patch(root, patch_path)
                action = "accept"
            else:
                pipeline.reject_patch(root, patch_path)
                action = "reject"

        latency = None if detected_at is None else detected_at - bug_introduced_at

        return ScenarioResult(
            scenario=scenario,
            changesets=tuple(changesets),
            detected=detected_at is not None,
            detection_latency_seconds=latency,
            action=action,
        )


@dataclass(frozen=True)
class EvaluationReport:
    """Aggregate detection metrics across a batch of single-pass replays."""

    results: tuple[ScenarioResult, ...] = field(default_factory=tuple)

    @property
    def true_positives(self) -> int:
        return sum(1 for result in self.results if result.true_positive)

    @property
    def false_negatives(self) -> int:
        return sum(1 for result in self.results if result.false_negative)

    @property
    def false_interruptions(self) -> int:
        return sum(1 for result in self.results if result.false_interruption)

    @property
    def clean_scenario_count(self) -> int:
        return sum(1 for result in self.results if not result.scenario.bug_expected)

    @property
    def precision(self) -> float | None:
        denominator = self.true_positives + self.false_interruptions
        return None if denominator == 0 else self.true_positives / denominator

    @property
    def false_interruption_rate(self) -> float | None:
        if self.clean_scenario_count == 0:
            return None
        return self.false_interruptions / self.clean_scenario_count

    @property
    def accept_rate(self) -> float | None:
        accepts = sum(1 for result in self.results if result.action == "accept")
        total_actions = sum(1 for result in self.results if result.action is not None)
        return None if total_actions == 0 else accepts / total_actions

    @property
    def mean_detection_latency_seconds(self) -> float | None:
        latencies = [
            result.detection_latency_seconds
            for result in self.results
            if result.true_positive and result.detection_latency_seconds is not None
        ]
        return None if not latencies else sum(latencies) / len(latencies)


def run_suite(
    scenarios: tuple[Scenario, ...],
    debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    idle_seconds: float = _DEFAULT_IDLE_SECONDS,
) -> EvaluationReport:
    """Replay every scenario once and aggregate the results into a report."""
    return EvaluationReport(
        results=tuple(
            run_scenario(scenario, debounce_seconds=debounce_seconds, idle_seconds=idle_seconds)
            for scenario in scenarios
        )
    )


@dataclass(frozen=True)
class IterationOutcome:
    """One pass of a convergence replay: did it verify, and what did the
    interruption policy decide."""

    verified: bool
    decision: InterruptionDecision | None
    action: str | None


@dataclass(frozen=True)
class ConvergenceResult:
    """Repeated replays of one bug-and-fix scenario, tracking how the
    interruption policy's verdict evolves as accept signals accumulate."""

    scenario: Scenario
    iterations: tuple[IterationOutcome, ...] = ()

    @property
    def first_interrupt_iteration(self) -> int | None:
        """1-based index of the first iteration the policy chose to surface, if any."""
        for index, iteration in enumerate(self.iterations, start=1):
            if iteration.decision is not None and iteration.decision.interrupt:
                return index
        return None


def run_convergence(
    scenario: Scenario,
    repeats: int = 5,
    debounce_seconds: float = _DEFAULT_DEBOUNCE_SECONDS,
    idle_seconds: float = _DEFAULT_IDLE_SECONDS,
) -> ConvergenceResult:
    """Replay the same bug-and-fix scenario `repeats` times, accepting every
    verified fix, and record how `InterruptionPolicy`'s verdict evolves.

    Needs a scenario with exactly one edit and a known regression test/fix —
    the same bug has to be reintroducible every iteration.
    """
    if not scenario.bug_expected or not scenario.regression_test or not scenario.proposed_fix:
        raise ValueError("convergence replay needs a scenario with a known bug and fix")
    if len(scenario.session.edits) != 1:
        raise ValueError("convergence replay only supports a single-edit scenario")

    bug_edit = scenario.session.edits[0]

    with TemporaryDirectory(prefix="shadowci-eval-") as tmp:
        root = Path(tmp) / "repo"
        _init_repo(root, scenario.session.initial_files)

        config = _build_config(root, debounce_seconds, idle_seconds)
        worktree = ShadowWorktree(config)
        worktree.ensure_created()
        detector = ChangeSetDetector(config)

        def create_provider(_name: str) -> ScriptedProvider:
            return ScriptedProvider(tests=[scenario.regression_test], fixes=[scenario.proposed_fix])

        live_path = root / bug_edit.relative_path
        iterations: list[IterationOutcome] = []

        for iteration_index in range(repeats):
            # A ghost patch's new-test-file hunk can only apply once per
            # name — clear last iteration's before reintroducing the bug.
            for ghost_test in root.glob("tests/test_shadowci_ghost_*.py"):
                ghost_test.unlink()

            timestamp = iteration_index * (idle_seconds + 1.0)
            live_path.write_text(bug_edit.content or "")
            detector.add_events(
                [FileChangeEvent(path=live_path, kind=bug_edit.kind, timestamp=timestamp)]
            )

            now = timestamp + idle_seconds + _IDLE_PADDING_SECONDS
            changeset = detector.poll(now=now)
            if changeset is None:
                iterations.append(IterationOutcome(verified=False, decision=None, action=None))
                continue

            worktree.sync(list(changeset.events))
            _clear_bytecode_cache(worktree.path)
            result = pipeline.run_checks_and_maybe_escalate(
                changeset, worktree, config, create_provider
            )

            if result.ghost_patch is None or result.ghost_patch_path is None:
                iterations.append(
                    IterationOutcome(verified=False, decision=result.decision, action=None)
                )
                continue

            pipeline.accept_patch(root, result.ghost_patch_path)
            iterations.append(
                IterationOutcome(verified=True, decision=result.decision, action="accept")
            )

        return ConvergenceResult(scenario=scenario, iterations=tuple(iterations))
