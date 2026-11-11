"""The check-escalate-verify pipeline and ghost-patch disk layout, UI-free.

Through Phase 4 this logic lived inline in `cli.py`, printing as it went.
Phase 5 adds a second driver — the Bug-Radar TUI (`radar.py` / `tui.py`) —
that needs the exact same pipeline but as structured state instead of
stdout, and the exact same accept/reject/ignore behavior as the
`apply-ghost` / `reject-ghost` / `ignore-ghost` commands but triggered from
a keypress instead of a CLI invocation. Both drivers import from here
instead of each growing their own copy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shadowci.checks.suite import build_check_runners, run_checks
from shadowci.config import ShadowCIConfig
from shadowci.escalation import EscalationPolicy
from shadowci.events import ChangeSet, CheckResult, GhostPatch, Signal
from shadowci.ghost import (
    GhostPatchBuilder,
    GhostPatchError,
    apply_to_live_tree,
    check_applies_to_live_tree,
)
from shadowci.interruption import InterruptionDecision, InterruptionPolicy, fingerprint_for_patch
from shadowci.providers.base import LLMProvider
from shadowci.shadow_worktree import ShadowWorktree
from shadowci.signals import SignalStore

GHOST_PATCH_DIRNAME = ".shadowci/ghost"
SIGNALS_FILENAME = ".shadowci/signals.json"


def ghost_patch_dir(root: Path) -> Path:
    path = root / GHOST_PATCH_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def signal_store(root: Path) -> SignalStore:
    return SignalStore(root / SIGNALS_FILENAME)


def metadata_path(patch_path: Path) -> Path:
    return patch_path.with_suffix(".json")


def load_patch_metadata(patch_path: Path) -> dict | None:
    """The fingerprint/digest sidecar saved alongside a ghost patch, if any.

    Patch files handed in by hand (as in a scripted apply) won't have
    one — in that case the signal simply isn't recorded, rather than
    guessing a fingerprint.
    """
    path = metadata_path(patch_path)
    if not path.is_file():
        return None
    return json.loads(path.read_text())


def _patch_target_files(patch: GhostPatch, root: Path) -> list[str]:
    files = []
    for path in patch.changeset.paths:
        try:
            files.append(path.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            files.append(str(path))
    return sorted(files)


def _retire_superseded_patches(
    ghost_dir: Path, new_patch_path: Path, files: list[str]
) -> tuple[str, ...]:
    """Remove older pending patches that target the same set of files.

    No signal is recorded for a patch retired this way — it was never
    accepted, rejected, or ignored, just displaced by a newer fix.
    """
    retired = []
    for patch_path in sorted(ghost_dir.glob("*.patch")):
        if patch_path == new_patch_path:
            continue
        metadata = load_patch_metadata(patch_path)
        if metadata is None or metadata.get("files") != files:
            continue
        metadata_path(patch_path).unlink(missing_ok=True)
        patch_path.unlink(missing_ok=True)
        retired.append(patch_path.name)
    return tuple(retired)


def save_ghost_patch(patch: GhostPatch, root: Path) -> tuple[Path, tuple[str, ...]]:
    digest = hashlib.sha1(patch.diff.encode("utf-8")).hexdigest()[:10]
    ghost_dir = ghost_patch_dir(root)
    patch_path = ghost_dir / f"{digest}.patch"
    files = _patch_target_files(patch, root)
    patch_path.write_text(patch.diff)
    metadata_path(patch_path).write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint_for_patch(patch),
                "summary": patch.evidence.summary,
                "files": files,
            },
            indent=2,
        )
    )
    retired = _retire_superseded_patches(ghost_dir, patch_path, files)
    return patch_path, retired


@dataclass(frozen=True)
class GhostPatchListing:
    """One saved ghost patch as seen by `ghosts`: where it lives, what it
    claims to do, and whether it would still apply to the live tree."""

    digest: str
    patch_path: Path
    metadata: dict | None
    ready: bool


def list_ghost_patches(root: Path) -> tuple[GhostPatchListing, ...]:
    """Every saved ghost patch under `root`, newest first, each checked
    against the live tree right now (`ready` vs `stale`)."""
    ghost_dir = root / GHOST_PATCH_DIRNAME
    if not ghost_dir.is_dir():
        return ()

    patch_paths = sorted(
        ghost_dir.glob("*.patch"), key=lambda path: path.stat().st_mtime, reverse=True
    )
    listings = []
    for patch_path in patch_paths:
        diff = patch_path.read_text()
        listings.append(
            GhostPatchListing(
                digest=patch_path.stem,
                patch_path=patch_path,
                metadata=load_patch_metadata(patch_path),
                ready=check_applies_to_live_tree(diff, root),
            )
        )
    return tuple(listings)


def latest_ready_ghost_patch(root: Path) -> Path | None:
    """The newest saved ghost patch that still applies cleanly, if any."""
    for listing in list_ghost_patches(root):
        if listing.ready:
            return listing.patch_path
    return None


def clean_ghost_patches(root: Path) -> tuple[str, ...]:
    """Remove every saved ghost patch that no longer applies to the live
    tree. Stale patches are discarded outright, not rejected — no signal
    is recorded, since the user never actually saw or judged them."""
    removed = []
    for listing in list_ghost_patches(root):
        if listing.ready:
            continue
        metadata_path(listing.patch_path).unlink(missing_ok=True)
        listing.patch_path.unlink(missing_ok=True)
        removed.append(listing.patch_path.name)
    return tuple(removed)


@dataclass(frozen=True)
class PipelineOutcome:
    """What one run of checks (+ maybe escalation) against a change set produced."""

    check_results: tuple[CheckResult, ...] = ()
    failing_checks: tuple[CheckResult, ...] = ()
    provider_name: str | None = None
    unknown_provider_error: str | None = None
    provider_error: str | None = None
    regression_test: str | None = None
    proposed_fix: str | None = None
    ghost_patch: GhostPatch | None = None
    ghost_patch_path: Path | None = None
    ghost_patch_error: str | None = None
    retired_ghost_patches: tuple[str, ...] = ()
    decision: InterruptionDecision | None = None


def run_checks_and_maybe_escalate(
    changeset: ChangeSet,
    worktree: ShadowWorktree,
    config: ShadowCIConfig,
    create_provider: Callable[[str], LLMProvider],
) -> PipelineOutcome:
    """Run deterministic checks, escalate on failure, verify + save a ghost patch.

    `create_provider` is passed in (rather than imported directly) so a
    caller's module-level reference to it stays monkeypatchable in tests —
    this function reads whatever the caller currently has bound to that name.
    """
    runners = build_check_runners(changeset, worktree.path)
    results = run_checks(runners, worktree.path)

    failing = tuple(result for result in results if not result.passed)
    if not failing:
        return PipelineOutcome(check_results=results, failing_checks=failing)

    try:
        provider = create_provider(config.provider)
    except ValueError as exc:
        return PipelineOutcome(
            check_results=results, failing_checks=failing, unknown_provider_error=str(exc)
        )

    try:
        escalation_policy = EscalationPolicy(provider)
        escalation = escalation_policy.maybe_escalate(changeset, results)
    except RuntimeError as exc:
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            provider_error=str(exc),
        )

    if escalation is None:
        return PipelineOutcome(
            check_results=results, failing_checks=failing, provider_name=provider.name
        )

    try:
        builder = GhostPatchBuilder(worktree)
        patch = builder.build(
            changeset,
            escalation.failing_checks,
            escalation.regression_test,
            escalation.proposed_fix,
        )
    except GhostPatchError as exc:
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            regression_test=escalation.regression_test,
            proposed_fix=escalation.proposed_fix,
            ghost_patch_error=str(exc),
        )

    patch_path, retired = save_ghost_patch(patch, config.project_root)
    interruption_policy = InterruptionPolicy(
        signal_store(config.project_root),
        threshold=config.interruption_threshold,
        min_signals=config.interruption_min_signals,
    )
    decision = interruption_policy.evaluate(patch)

    return PipelineOutcome(
        check_results=results,
        failing_checks=failing,
        provider_name=provider.name,
        regression_test=escalation.regression_test,
        proposed_fix=escalation.proposed_fix,
        ghost_patch=patch,
        ghost_patch_path=patch_path,
        retired_ghost_patches=retired,
        decision=decision,
    )


@dataclass(frozen=True)
class ActionResult:
    """What happened when a saved ghost patch was accepted, rejected, or ignored."""

    ok: bool
    message: str
    fingerprint: str | None = None


def accept_patch(root: Path, patch_file: Path) -> ActionResult:
    """Apply a saved ghost patch to the live tree and record an accept signal."""
    if not patch_file.is_file():
        return ActionResult(False, f"no such patch file: {patch_file}")

    diff = patch_file.read_text()
    try:
        apply_to_live_tree(diff, root)
    except GhostPatchError as exc:
        return ActionResult(False, str(exc))

    lines = [f"applied {patch_file} to {root}"]
    fingerprint = None
    metadata = load_patch_metadata(patch_file)
    if metadata is not None:
        signal_store(root).record(metadata["fingerprint"], Signal.ACCEPT, metadata["digest"])
        fingerprint = metadata["fingerprint"]
        lines.append(f"recorded signal: accept (fingerprint={fingerprint!r})")
        metadata_path(patch_file).unlink(missing_ok=True)

    patch_file.unlink(missing_ok=True)
    return ActionResult(True, "\n".join(lines), fingerprint)


def reject_patch(root: Path, patch_file: Path) -> ActionResult:
    """Discard a saved ghost patch and record a reject signal."""
    if not patch_file.is_file():
        return ActionResult(False, f"no such patch file: {patch_file}")

    lines = []
    fingerprint = None
    metadata = load_patch_metadata(patch_file)
    if metadata is not None:
        signal_store(root).record(metadata["fingerprint"], Signal.REJECT, metadata["digest"])
        fingerprint = metadata["fingerprint"]
        lines.append(f"recorded signal: reject (fingerprint={fingerprint!r})")
        metadata_path(patch_file).unlink(missing_ok=True)
    else:
        lines.append("no fingerprint metadata found alongside this patch — signal not recorded")

    patch_file.unlink()
    lines.append(f"discarded {patch_file}")

    return ActionResult(True, "\n".join(lines), fingerprint)


def ignore_patch(root: Path, patch_file: Path) -> ActionResult:
    """Leave a saved ghost patch on disk, unapplied, and record an ignore signal."""
    if not patch_file.is_file():
        return ActionResult(False, f"no such patch file: {patch_file}")

    metadata = load_patch_metadata(patch_file)
    if metadata is None:
        return ActionResult(
            True, "no fingerprint metadata found alongside this patch — signal not recorded"
        )

    signal_store(root).record(metadata["fingerprint"], Signal.IGNORE, metadata["digest"])
    message = (
        f"recorded signal: ignore (fingerprint={metadata['fingerprint']!r})\n"
        f"{patch_file} left on disk, unapplied"
    )
    return ActionResult(True, message, metadata["fingerprint"])
