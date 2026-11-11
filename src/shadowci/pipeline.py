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

import difflib
import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shadowci.checks.base import clear_bytecode_cache
from shadowci.checks.lint_runner import RuffRunner
from shadowci.checks.pytest_runner import PytestRunner
from shadowci.checks.suite import build_check_runners, run_checks
from shadowci.checks.targeted import find_targeted_tests
from shadowci.checks.type_checker import MypyRunner
from shadowci.config import ShadowCIConfig
from shadowci.escalation import EscalationPolicy
from shadowci.events import ChangeSet, CheckResult, EvidenceCard, GhostPatch, Signal
from shadowci.ghost import (
    GHOST_TEST_FILENAME_PREFIX,
    GhostPatchBuilder,
    GhostPatchError,
    apply_to_live_tree,
    check_applies_to_live_tree,
)
from shadowci.interruption import InterruptionDecision, InterruptionPolicy, fingerprint_for_patch
from shadowci.providers.base import LLMProvider
from shadowci.revert_check import fix_reverts_a_pure_addition, is_crash_failure
from shadowci.shadow_worktree import ShadowWorktree
from shadowci.signals import SignalStore
from shadowci.test_expectation import TestExpectationError, build_updated_test_source

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


def save_ghost_patch(
    patch: GhostPatch,
    root: Path,
    *,
    kind: str = "fix",
    group: str | None = None,
    files: list[str] | None = None,
) -> tuple[Path, tuple[str, ...]]:
    """Save a verified `GhostPatch` to disk.

    `kind` is "fix" for a normal, single auto-saved patch, or "question"
    for one of two mutually exclusive options saved when a fix would
    revert the user's own intentional change (issue #37) — `group` then
    ties the pair together. Old patch files saved before these fields
    existed have neither key and still load and apply as `kind: "fix"`.

    `files` overrides the target-files list that's normally derived from
    `patch.changeset` — needed for the "update the test" question option,
    whose own target (the test file) isn't the changeset that triggered
    it (the source file the user edited), so the two options don't look
    like competing patches for the same file and retire one another.
    """
    digest = hashlib.sha1(patch.diff.encode("utf-8")).hexdigest()[:10]
    ghost_dir = ghost_patch_dir(root)
    patch_path = ghost_dir / f"{digest}.patch"
    files = files if files is not None else _patch_target_files(patch, root)
    patch_path.write_text(patch.diff)
    metadata = {
        "digest": digest,
        "fingerprint": fingerprint_for_patch(patch),
        "summary": patch.evidence.summary,
        "files": files,
        "kind": kind,
    }
    if group is not None:
        metadata["group"] = group
    metadata_path(patch_path).write_text(json.dumps(metadata, indent=2))
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


def _git_diff_head(worktree_path: Path) -> str:
    """The user's own edit, as seen in the shadow worktree, before any
    proposed fix is applied there — issue #37's revert check compares a
    fix against exactly this, captured up front."""
    result = subprocess.run(
        ["git", "diff", "HEAD"], cwd=worktree_path, capture_output=True, text=True
    )
    return result.stdout if result.returncode == 0 else ""


def _modify_file_diff(relative_path: Path, old_content: str, new_content: str) -> str:
    """A git-apply-compatible unified diff that rewrites an existing file."""
    rel = relative_path.as_posix()
    diff = "".join(
        difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
        )
    )
    if diff and not diff.endswith("\n"):
        diff += "\n"
    return diff


def _build_update_test_option(
    worktree: ShadowWorktree,
    changeset: ChangeSet,
    failing_checks: tuple[CheckResult, ...],
) -> tuple[GhostPatch, str] | None:
    """Try to build the "update the test" option: rewrite the real failing
    test's expectation to match the user's new behavior, verified for real
    in the shadow worktree. Returns the patch plus its target file's
    worktree-relative path (used instead of `changeset` to compute which
    *file* this option targets — it's the test file, not what the
    changeset edited), or None when the test isn't a shape this can
    rewrite deterministically, or the rewrite doesn't make the full suite
    pass — the caller falls back to an ordinary verified fix then.
    """
    targets = [
        path
        for path in find_targeted_tests(changeset, worktree.path, "tests")
        if not path.name.startswith(GHOST_TEST_FILENAME_PREFIX)
    ]
    if len(targets) != 1:
        return None
    test_path = targets[0]
    test_relative = test_path.relative_to(worktree.path)
    original_source = test_path.read_text()

    try:
        updated_source = build_updated_test_source(original_source, worktree.path)
    except TestExpectationError:
        return None

    # the "keep old behaviour" option (verified just before this one is
    # attempted) left its own ghost regression test behind — it still
    # expects the *old* behavior, so it must not be in the way while this
    # option confirms the suite passes with the behavior kept as-is.
    for ghost_test in worktree.path.rglob(f"{GHOST_TEST_FILENAME_PREFIX}*.py"):
        ghost_test.unlink()

    test_path.write_text(updated_source)
    clear_bytecode_cache(worktree.path)
    try:
        confirmation = run_checks(
            [PytestRunner(), MypyRunner(), RuffRunner()], worktree.path
        )
    finally:
        test_path.write_text(original_source)

    if any(not result.passed for result in confirmation):
        return None

    diff = _modify_file_diff(test_relative, original_source, updated_source)
    evidence = EvidenceCard(
        summary=f"update {test_relative.as_posix()} to match the new behavior",
        failing_test_before=original_source,
        check_results=confirmation,
        originally_failed_checks=tuple(result.name for result in failing_checks),
    )
    patch = GhostPatch(diff=diff, evidence=evidence, changeset=changeset)
    return patch, test_relative.as_posix()


def _retire_question_sibling(root: Path, metadata: dict) -> None:
    """When one option of a question pair (issue #37) is resolved, discard
    the other without recording a signal for it — it was never
    independently shown to, or judged by, the user."""
    group = metadata.get("group")
    if metadata.get("kind") != "question" or group is None:
        return
    for sibling_path in ghost_patch_dir(root).glob("*.patch"):
        if sibling_path.stem == metadata.get("digest"):
            continue
        sibling_metadata = load_patch_metadata(sibling_path)
        if sibling_metadata is None or sibling_metadata.get("group") != group:
            continue
        metadata_path(sibling_path).unlink(missing_ok=True)
        sibling_path.unlink(missing_ok=True)


@dataclass(frozen=True)
class QuestionOption:
    """One verified, mutually exclusive option offered when a fix would
    revert the user's own intentional change (issue #37)."""

    label: str
    patch: GhostPatch
    patch_path: Path
    digest: str


@dataclass(frozen=True)
class QuestionOutcome:
    """Two verified options saved instead of a single auto-accepted fix,
    because the "natural" fix would have put back the user's own edit."""

    group: str
    keep_old_behaviour: QuestionOption
    update_the_test: QuestionOption


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
    question: QuestionOutcome | None = None


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

    # issue #37: known ahead of any fix — the user's own edit, exactly as
    # it sits in the shadow worktree right now, before anything is reverted.
    user_diff = _git_diff_head(worktree.path)
    ask_candidate = not is_crash_failure(failing) and fix_reverts_a_pure_addition(
        user_diff, escalation.proposed_fix
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

    update_test_option = None
    if ask_candidate:
        # the revert fix just verified above left the shadow worktree with
        # the change reverted — put the user's actual edit back before
        # trying to derive its real behavior for the second option.
        worktree.sync(list(changeset.events))
        clear_bytecode_cache(worktree.path)
        update_test_option = _build_update_test_option(worktree, changeset, failing)

    if ask_candidate and update_test_option is not None:
        update_test_patch, update_test_relative = update_test_option
        group = hashlib.sha1(
            (patch.diff + "\x00" + update_test_patch.diff).encode("utf-8")
        ).hexdigest()[:10]
        keep_path, keep_retired = save_ghost_patch(
            patch, config.project_root, kind="question", group=group
        )
        update_path, update_retired = save_ghost_patch(
            update_test_patch,
            config.project_root,
            kind="question",
            group=group,
            files=[update_test_relative],
        )
        question = QuestionOutcome(
            group=group,
            keep_old_behaviour=QuestionOption(
                "keep old behaviour", patch, keep_path, keep_path.stem
            ),
            update_the_test=QuestionOption(
                "update the test", update_test_patch, update_path, update_path.stem
            ),
        )
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            regression_test=escalation.regression_test,
            proposed_fix=escalation.proposed_fix,
            retired_ghost_patches=keep_retired + update_retired,
            question=question,
        )

    patch_path, retired = save_ghost_patch(patch, config.project_root, kind="fix")
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
        _retire_question_sibling(root, metadata)
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
