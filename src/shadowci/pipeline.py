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
from shadowci.ghost import GhostPatchBuilder, GhostPatchError, apply_to_live_tree
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


def save_ghost_patch(patch: GhostPatch, root: Path) -> Path:
    digest = hashlib.sha1(patch.diff.encode("utf-8")).hexdigest()[:10]
    patch_path = ghost_patch_dir(root) / f"{digest}.patch"
    patch_path.write_text(patch.diff)
    metadata_path(patch_path).write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint_for_patch(patch),
                "summary": patch.evidence.summary,
            },
            indent=2,
        )
    )
    return patch_path


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

    patch_path = save_ghost_patch(patch, config.project_root)
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
