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
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shadowci.checks.suite import build_check_runners, run_checks
from shadowci.classification import FailureClass, classify_failure
from shadowci.config import ShadowCIConfig
from shadowci.diffing import diff_touched_files, fix_reverts_user_change
from shadowci.escalation import EscalationPolicy
from shadowci.events import ChangeSet, CheckResult, GhostPatch, Signal
from shadowci.ghost import (
    GhostPatchBuilder,
    GhostPatchError,
    apply_to_live_tree,
    check_applies_to_live_tree,
)
from shadowci.interruption import InterruptionDecision, InterruptionPolicy, fingerprint_for_patch
from shadowci.pause import PAUSE_MESSAGE, current_pause, file_has_wip_marker
from shadowci.providers.base import LLMProvider
from shadowci.revert_check import (
    build_update_test_patch,
    diff_head,
    diff_head_reversed,
    is_crash_failure,
    is_intentional_reply,
)
from shadowci.shadow_worktree import ShadowWorktree
from shadowci.signals import SignalStore

GHOST_PATCH_DIRNAME = ".shadowci/ghost"
SIGNALS_FILENAME = ".shadowci/signals.json"

# Issue #53: while any of these exist in the live tree's git dir, a rebase,
# merge, cherry-pick or bisect is still in progress — the live tree is
# mid-operation, not mid-edit, so the shadow must not be synced, rebaselined,
# or checked until it's over.
_IN_PROGRESS_GIT_MARKERS = (
    ("rebase-merge", "a rebase is in progress"),
    ("rebase-apply", "a rebase is in progress"),
    ("MERGE_HEAD", "a merge is in progress"),
    ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
    ("BISECT_LOG", "a bisect is in progress"),
)


def _git_dir(root: Path) -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    git_dir = Path(result.stdout.strip())
    return git_dir if git_dir.is_absolute() else root / git_dir


def in_progress_git_operation(root: Path) -> str | None:
    """A human-readable description of a rebase/merge/cherry-pick/bisect in
    progress in `root`, or `None`. See `_IN_PROGRESS_GIT_MARKERS`."""
    git_dir = _git_dir(root)
    if git_dir is None:
        return None
    for marker, description in _IN_PROGRESS_GIT_MARKERS:
        if (git_dir / marker).exists():
            return description
    return None


def _head_sha(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _live_dirty_relative_paths(root: Path) -> set[str]:
    """Every path `git ls-files` reports as modified/untracked/deleted in
    the live tree right now — the genuinely uncommitted paths."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--modified", "--others", "--deleted", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return set()
    return {part for part in result.stdout.split("\0") if part}


def ensure_mirrored_at_start(worktree: ShadowWorktree, config: ShadowCIConfig) -> None:
    """Bring the shadow worktree in line with the live tree once, before any
    polling begins (issue #53): creates it if missing, then rebaselines it
    unconditionally so a change made while nothing was watching is already
    mirrored. A no-op while a rebase/merge/cherry-pick/bisect is in progress
    on the live tree — the first change set ready once it ends will trigger
    the same rebaseline via `sync_shadow_for_changeset`."""
    if in_progress_git_operation(config.project_root) is not None:
        return
    worktree.ensure_created()
    worktree.rebaseline()


@dataclass(frozen=True)
class MirrorSyncResult:
    """What happened when the shadow was brought in line with the live tree
    for one ready change set (issue #53)."""

    skipped_in_progress: str | None = None
    rebaselined_to: str | None = None  # short live sha, if a rebaseline happened
    git_only_change: bool = False
    touched: tuple[Path, ...] = ()


def sync_shadow_for_changeset(
    changeset: ChangeSet, worktree: ShadowWorktree, config: ShadowCIConfig
) -> MirrorSyncResult:
    """Keep the shadow worktree mirrored for one ready change set.

    Only ever spawns git to check `HEAD` when a change set is actually
    ready — never on an idle poll cycle. If the live tree's `HEAD` has
    moved since the shadow was last rebaselined (a commit, pull, checkout,
    or rebase), the shadow is rebaselined now; if every path in this change
    set turns out to exactly match the new `HEAD` (the change set exists
    only because of that git operation, not a user edit), the caller must
    not run checks or escalate for it.
    """
    in_progress = in_progress_git_operation(config.project_root)
    if in_progress is not None:
        return MirrorSyncResult(skipped_in_progress=in_progress)

    live_sha = _head_sha(config.project_root)
    shadow_sha = _head_sha(worktree.path) if worktree.exists() else None

    rebaselined_to: str | None = None
    git_only_change = False
    if live_sha is not None and live_sha != shadow_sha:
        worktree.rebaseline()
        rebaselined_to = live_sha[:10]

        dirty = _live_dirty_relative_paths(config.project_root)
        changed_relative: set[str] = set()
        for path in changeset.paths:
            try:
                changed_relative.add(
                    path.resolve().relative_to(config.project_root.resolve()).as_posix()
                )
            except ValueError:
                pass
        git_only_change = changed_relative.isdisjoint(dirty)

    touched: tuple[Path, ...] = ()
    if not git_only_change:
        touched = tuple(worktree.sync(list(changeset.events)))

    return MirrorSyncResult(
        rebaselined_to=rebaselined_to,
        git_only_change=git_only_change,
        touched=touched,
    )


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


def _escalation_paused_message(
    changeset: ChangeSet, worktree_path: Path, project_root: Path
) -> str | None:
    """Why escalation should be held back for this change set, if at all."""
    if current_pause(project_root) is not None:
        return PAUSE_MESSAGE

    for path in changeset.paths:
        try:
            relative = path.resolve().relative_to(project_root.resolve())
        except ValueError:
            relative = path
        candidate = worktree_path / relative
        if candidate.is_file() and file_has_wip_marker(candidate):
            return PAUSE_MESSAGE
    return None


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
    patch: GhostPatch, root: Path, failure_class: str | None = None
) -> tuple[Path, tuple[str, ...]]:
    digest = hashlib.sha1(patch.diff.encode("utf-8")).hexdigest()[:10]
    ghost_dir = ghost_patch_dir(root)
    patch_path = ghost_dir / f"{digest}.patch"
    files = _patch_target_files(patch, root)
    patch_path.write_text(patch.diff)
    metadata_path(patch_path).write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint_for_patch(patch, failure_class),
                "summary": patch.evidence.summary,
                "files": files,
                "kind": "fix",
            },
            indent=2,
        )
    )
    retired = _retire_superseded_patches(ghost_dir, patch_path, files)
    return patch_path, retired


def save_question_patch(
    patch: GhostPatch, root: Path, group: str, label: str, failure_class: str | None = None
) -> Path:
    """Save one verified option of a two-option question (issue #37):
    `kind: "question"` instead of `"fix"`, sharing `group` with its
    sibling so `accept_patch` can retire it without recording a signal."""
    digest = hashlib.sha1(f"{group}:{label}:{patch.diff}".encode()).hexdigest()[:10]
    ghost_dir = ghost_patch_dir(root)
    patch_path = ghost_dir / f"{digest}.patch"
    files = _patch_target_files(patch, root)
    patch_path.write_text(patch.diff)
    metadata_path(patch_path).write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint_for_patch(patch, failure_class),
                "summary": patch.evidence.summary,
                "files": files,
                "kind": "question",
                "group": group,
                "label": label,
            },
            indent=2,
        )
    )
    return patch_path


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
class GhostQuestion:
    """Two verified options surfaced instead of a silent fix (issue #37),
    for a fix that would otherwise put back the user's own edit."""

    group: str
    message: str
    keep_old_behavior_path: Path
    update_test_path: Path


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
    question: GhostQuestion | None = None
    paused_message: str | None = None


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

    # Issue #40: "this is mid-feature, don't judge it yet" — a project-wide
    # `shadowci pause` or a `# shadowci: wip` marker on a touched file holds
    # back the model call entirely. Checks above still ran and still synced
    # into the shadow worktree; only escalation is skipped, so no provider
    # is ever constructed or called for this change set.
    paused_message = _escalation_paused_message(changeset, worktree.path, config.project_root)
    if paused_message is not None:
        return PipelineOutcome(
            check_results=results, failing_checks=failing, paused_message=paused_message
        )

    classification = classify_failure(changeset, failing, worktree.path, config.project_root)
    failure_class = (
        classification.failure_class.value if classification.failure_class else None
    )

    try:
        provider = create_provider(config.provider)
    except ValueError as exc:
        return PipelineOutcome(
            check_results=results, failing_checks=failing, unknown_provider_error=str(exc)
        )
    provider.configure_prompt_context(worktree.path, config.project_root, classification)

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

    # Issue #38: a test that was already failing at HEAD isn't a regression
    # of this edit at all — the provider has already been consulted above
    # (so a bad/erroring provider is still reported the same way it always
    # was), but nothing gets built or saved for it.
    if classification.failure_class is FailureClass.PRE_EXISTING:
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            regression_test=escalation.regression_test,
            proposed_fix=escalation.proposed_fix,
        )

    # Issue #39: the provider may decline to propose a fix at all, replying
    # with the single line `INTENTIONAL: <reason>` when the failure looks
    # like a deliberate behavior change. There's no diff to apply in that
    # case — the deterministic stand-in is the exact inverse of the user's
    # own uncommitted edit, i.e. the fix that puts it back.
    intentional_reply = is_intentional_reply(escalation.proposed_fix)
    fix_diff = diff_head_reversed(worktree.path) if intentional_reply else escalation.proposed_fix

    # Issue #37: a fix that puts back the user's own edit, for a non-crash
    # failure, must not be saved as a silent "verified fix" — unless we
    # can't actually offer a deterministic second option, in which case
    # there's nothing to ask and the normal fix flow below still applies.
    update_test_patch: GhostPatch | None = None
    if not is_crash_failure(failing) and (
        intentional_reply or fix_reverts_user_change(diff_head(worktree.path), escalation.proposed_fix)
    ):
        update_test_patch = build_update_test_patch(worktree, changeset, failing)

    try:
        builder = GhostPatchBuilder(worktree)
        patch = builder.build(
            changeset,
            escalation.failing_checks,
            escalation.regression_test,
            fix_diff,
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

    # Issue #38: when the user edited the failing test file itself, that
    # test is the new spec — a patch is never saved if it also touches a
    # test file the user edited, no matter how cleanly it verified.
    excluded_test_files = set(classification.edited_test_files)
    if excluded_test_files and excluded_test_files & diff_touched_files(patch.diff):
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            regression_test=escalation.regression_test,
            proposed_fix=escalation.proposed_fix,
            ghost_patch_error=(
                "proposed fix touches a test file you edited — refusing to save it; "
                "the edited test is the new spec and must not be patched"
            ),
        )

    if update_test_patch is not None:
        question = _save_question(
            changeset, config.project_root, patch, update_test_patch, failure_class
        )
        return PipelineOutcome(
            check_results=results,
            failing_checks=failing,
            provider_name=provider.name,
            regression_test=escalation.regression_test,
            proposed_fix=escalation.proposed_fix,
            question=question,
        )

    patch_path, retired = save_ghost_patch(patch, config.project_root, failure_class)
    interruption_policy = InterruptionPolicy(
        signal_store(config.project_root),
        threshold=config.interruption_threshold,
        min_signals=config.interruption_min_signals,
    )
    decision = interruption_policy.evaluate_fingerprint(
        fingerprint_for_patch(patch, failure_class)
    )

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


def _save_question(
    changeset: ChangeSet,
    root: Path,
    keep_old_behavior: GhostPatch,
    update_test: GhostPatch,
    failure_class: str | None = None,
) -> GhostQuestion:
    group = hashlib.sha1(
        (keep_old_behavior.diff + update_test.diff).encode("utf-8")
    ).hexdigest()[:10]
    keep_path = save_question_patch(
        keep_old_behavior, root, group, "keep old behaviour", failure_class
    )
    update_path = save_question_patch(update_test, root, group, "update the test", failure_class)

    changed_name = changeset.paths[0].stem if changeset.paths else "the module"
    test_files = _patch_target_files(update_test, root)
    test_name = Path(test_files[0]).stem if test_files else "the test"
    message = (
        f"Your change to {changed_name}() broke {test_name}. "
        "Update the test, or keep the old behavior?"
    )
    return GhostQuestion(
        group=group,
        message=message,
        keep_old_behavior_path=keep_path,
        update_test_path=update_path,
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
        sibling = _retire_question_sibling(root, metadata, patch_file)
        if sibling is not None:
            lines.append(f"retired sibling question option: {sibling}")

    patch_file.unlink(missing_ok=True)
    return ActionResult(True, "\n".join(lines), fingerprint)


def _retire_question_sibling(root: Path, metadata: dict, accepted_patch: Path) -> str | None:
    """When `accepted_patch` was one of a question's two options, remove
    the other option without recording any signal for it — it was never
    separately judged, just made moot by answering the question."""
    if metadata.get("kind") != "question":
        return None
    group = metadata.get("group")
    if group is None:
        return None

    for patch_path in sorted(ghost_patch_dir(root).glob("*.patch")):
        if patch_path == accepted_patch:
            continue
        sibling_metadata = load_patch_metadata(patch_path)
        if sibling_metadata is None or sibling_metadata.get("group") != group:
            continue
        metadata_path(patch_path).unlink(missing_ok=True)
        patch_path.unlink(missing_ok=True)
        return patch_path.name
    return None


def reject_patch(root: Path, patch_file: Path, reason: str | None = None) -> ActionResult:
    """Discard a saved ghost patch and record a reject signal.

    `reason` (issue #40) says *why* the patch was rejected — e.g.
    `"intentional-change"` when the patch was fine but the user's own
    uncommitted edit was deliberate. That reason is excluded from the
    interruption policy's scoring (see `InterruptionPolicy`) instead of
    counting against the fingerprint the way an ordinary reject does.
    """
    if not patch_file.is_file():
        return ActionResult(False, f"no such patch file: {patch_file}")

    lines = []
    fingerprint = None
    metadata = load_patch_metadata(patch_file)
    if metadata is not None:
        signal_store(root).record(
            metadata["fingerprint"], Signal.REJECT, metadata["digest"], reason=reason
        )
        fingerprint = metadata["fingerprint"]
        reason_suffix = f", reason={reason!r}" if reason else ""
        lines.append(f"recorded signal: reject (fingerprint={fingerprint!r}{reason_suffix})")
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
