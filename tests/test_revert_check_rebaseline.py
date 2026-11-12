"""Regression tests for issue #53, reproduction steps 1-6: once the shadow
worktree's `HEAD` is stale, `revert_check.diff_head`/`diff_head_reversed`
treat already-committed work as the user's own uncommitted edit.

`EscalationPolicy`/`pipeline.run_checks_and_maybe_escalate` compute "the
user's own edit" as `git diff HEAD` *inside the shadow worktree*
(`revert_check.diff_head`). When the shadow was never moved past the
commit it was created at, that diff includes everything committed since
then. For an `INTENTIONAL:` reply (issue #39), the "keep old behaviour"
patch is `git diff -R HEAD` in the shadow
(`revert_check.diff_head_reversed`) — the exact inverse — so it reverts
committed work too, and that patch applies cleanly to the live tree.

These tests reproduce that exact failure and pin the fix: once the shadow
worktree is rebaselined (`ShadowWorktree.rebaseline()`, added for issue
#53) before `diff_head`/`diff_head_reversed` run, both only ever see the
genuinely uncommitted edit — the committed feature is untouched. This is
exactly what `shadowci watch`/`radar`/`evaluate` must do before handing a
change set to `pipeline.run_checks_and_maybe_escalate`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers.scripted import ScriptedProvider
from shadowci.revert_check import diff_head_reversed
from shadowci.shadow_worktree import ShadowWorktree

MEDIAN_FUNCTION = (
    "\n\n"
    "def median(values):\n"
    "    s = sorted(values)\n"
    "    n = len(s)\n"
    "    mid = n // 2\n"
    "    if n % 2:\n"
    "        return s[mid]\n"
    "    return (s[mid - 1] + s[mid]) / 2\n"
)


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_keep_old_behaviour_patch_after_rebaseline_touches_only_the_uncommitted_edit(
    git_repo: Path,
):
    """Reproduction steps 2 and 5 from the issue, with the fix in place:
    committing a feature (`median`) while the shadow is kept rebaselined
    must not make `diff_head_reversed` (the "keep old behaviour" stand-in
    for an INTENTIONAL reply) remove it."""
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    live_app = git_repo / "app.py"
    live_app.write_text(live_app.read_text() + MEDIAN_FUNCTION)
    worktree.rebaseline()  # watch syncing the feature in, before it's committed
    _commit_all(git_repo, "add median")

    live_app.write_text(live_app.read_text() + "\n# a small, uncommitted comment\n")
    worktree.rebaseline()  # watch rebaselining after the commit, then overlaying the new edit

    patch = diff_head_reversed(worktree.path)

    # Only inspect the actually-removed lines, not the `@@ ... @@` hunk
    # header — git's diff includes the enclosing function's *signature* as
    # context there, so "median" legitimately appears in the header even
    # when none of its body is touched.
    removed_lines = [
        line for line in patch.splitlines() if line.startswith("-") and not line.startswith("---")
    ]
    assert not any("median" in line or "sorted(values)" in line for line in removed_lines), (
        "the keep-old-behaviour patch must not remove any line from the "
        f"already-committed median() function; got removed lines:\n{removed_lines}"
    )
    assert any("comment" in line for line in removed_lines)


def test_applying_the_keep_old_behaviour_patch_preserves_the_committed_feature(
    git_repo: Path,
):
    """End to end: applying the "keep old behaviour" patch to the live tree
    after a rebaseline must leave the committed feature intact — the exact
    opposite of reproduction step 5, where it deleted `median()`."""
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    live_app = git_repo / "app.py"
    live_app.write_text(live_app.read_text() + MEDIAN_FUNCTION)
    worktree.rebaseline()
    _commit_all(git_repo, "add median")

    live_app.write_text(live_app.read_text() + "\n# a small, uncommitted comment\n")
    worktree.rebaseline()

    patch = diff_head_reversed(worktree.path)
    patch_file = git_repo / "keep_old.patch"
    patch_file.write_text(patch)

    _run_git(["apply", str(patch_file)], cwd=git_repo)

    assert "median" in live_app.read_text(), "committed feature must survive the applied patch"
    assert "# a small, uncommitted comment" not in live_app.read_text()


AVERAGE_CLEAN = (
    "def average(values):\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)

AVERAGE_CHANGED = (
    "def average(values):\n"
    "    if not values:\n"
    "        return 0\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)

AVERAGE_TEST = (
    "import pytest\n\n"
    "from calc import average\n\n\n"
    "def test_average_of_empty_list_raises():\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        average([])\n"
)

AVERAGE_REVERT_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,5 +1,3 @@\n"
    " def average(values):\n"
    "-    if not values:\n"
    "-        return 0\n"
    "     total = sum(values)\n"
    "     return total / len(values)\n"
)


def test_pipeline_question_flow_keeps_committed_feature_after_rebaseline(
    git_repo: Path,
):
    """The two-option question flow from issue #37, with a feature
    (`median()`) committed in between the clean baseline and the
    intentional behavior change: as in the issue's reproduction, the
    shadow must be rebaselined before `diff_head`/`diff_head_reversed`
    run, so neither saved option touches the already-committed feature —
    only the uncommitted `average()` edit is "the user's own edit"."""
    module_relpath = "calc.py"
    test_relpath = "tests/test_calc.py"

    _write(git_repo, module_relpath, AVERAGE_CLEAN)
    _write(git_repo, test_relpath, AVERAGE_TEST)
    _commit_all(git_repo, "add calc.average and its test")

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    with_feature = AVERAGE_CLEAN + MEDIAN_FUNCTION
    module_path = _write(git_repo, module_relpath, with_feature)
    worktree.rebaseline()
    _commit_all(git_repo, "add median() to calc.py")

    changed_content = AVERAGE_CHANGED + MEDIAN_FUNCTION
    _write(git_repo, module_relpath, changed_content)
    worktree.rebaseline()

    changeset = ChangeSet(events=(FileChangeEvent(path=module_path, kind=ChangeKind.MODIFIED),))

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[AVERAGE_TEST], fixes=[AVERAGE_REVERT_FIX]),
    )

    all_listings = pipeline.list_ghost_patches(config.project_root)
    questions = [
        listing for listing in all_listings if (listing.metadata or {}).get("kind") == "question"
    ]
    assert len(questions) == 2, (
        f"expected a two-option question for this intentional-looking change, got "
        f"{[(l.patch_path.name, (l.metadata or {}).get('kind')) for l in all_listings]}; "
        f"ghost_patch_error={outcome.ghost_patch_error}"
    )
    for listing in questions:
        diff_text = listing.patch_path.read_text()
        assert "median" not in diff_text, (
            f"question option {listing.patch_path.name} must not touch the "
            f"already-committed median():\n{diff_text}"
        )
