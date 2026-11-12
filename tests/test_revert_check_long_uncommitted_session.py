"""Regression tests for issue #52: a long uncommitted session mixes an
earlier, already-green edit into "the user's own edit" for every edit
that follows it, because nothing was ever committed in between.

Issue #53 fixed the shadow worktree's `HEAD` going stale relative to the
*live* tree, but `revert_check.diff_head`/`diff_head_reversed`
(`revert_check.py` lines 76 and 95) and `providers.prompts`'s own
`git diff HEAD` (`prompts.py` line 75) still compare against the live
tree's last *commit* — so two edits with no commit between them are
indistinguishable from one. The first edit here (adding `median()`) is
finished, correct work whose checks already passed once; the second,
layered on top with still no commit in between, intentionally changes
`average()`'s behavior and breaks `test_average_of_empty_list_raises`.

Today `median()` — unrelated, already checked, and byte-for-byte
unchanged since the first edit — is wrongly folded into "the user's own
edit" for the second: the two-option question names it as part of what
broke the test, and the model prompts' diff section shows it to the model
as though it were part of the current edit. Once the fix lands (a
baseline snapshot taken after the last fully green run, advanced only
then), `median()` must disappear from both — only `average()`'s edit is
"the user's own edit" for the second change set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.evaluation.scenarios import intentional_behavior_change_scenario
from shadowci.providers.prompts import build_fix_prompt, build_test_prompt
from shadowci.providers.scripted import ScriptedProvider
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


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _intentional_change_pieces():
    scenario = intentional_behavior_change_scenario()
    edit = scenario.session.edits[0]
    module_relpath = edit.relative_path
    clean_content = scenario.session.initial_files[module_relpath]
    changed_content = edit.content
    test_relpath = next(
        path for path in scenario.session.initial_files if path != module_relpath
    )
    assert scenario.regression_test is not None
    assert scenario.proposed_fix is not None
    assert changed_content is not None
    return (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        scenario.regression_test,
        scenario.proposed_fix,
    )


def _changeset(path: Path) -> ChangeSet:
    return ChangeSet(events=(FileChangeEvent(path=path, kind=ChangeKind.MODIFIED),))


def _setup_two_edits_no_commit_between(git_repo: Path):
    """Reproduction from the issue: edit 1 (`median()`) is finished,
    correct, uncommitted work whose checks already passed once; edit 2,
    with still no commit in between, intentionally changes `average()`
    and breaks the old test. Returns everything a caller needs to inspect
    what the pipeline considers "the user's own edit" for edit 2."""
    (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        regression_test,
        proposed_fix,
    ) = _intentional_change_pieces()

    module_path = _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add average() and its test")

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    pipeline.ensure_mirrored_at_start(worktree, config)

    # Edit 1: finished, correct work -- add median(). No commit.
    with_median = clean_content + MEDIAN_FUNCTION
    module_path.write_text(with_median)
    changeset1 = _changeset(module_path)
    pipeline.sync_shadow_for_changeset(changeset1, worktree, config)
    outcome1 = pipeline.run_checks_and_maybe_escalate(
        changeset1, worktree, config, lambda name: ScriptedProvider(tests=[], fixes=[])
    )
    assert not outcome1.failing_checks, (
        f"edit 1 (median()) must pass every check on its own: {outcome1.failing_checks}"
    )

    # Edit 2: intentional behavior change to average(), still uncommitted,
    # layered on top of edit 1 -- no commit happened between them.
    with_both = changed_content + MEDIAN_FUNCTION
    module_path.write_text(with_both)
    changeset2 = _changeset(module_path)
    pipeline.sync_shadow_for_changeset(changeset2, worktree, config)

    return config, worktree, changeset2, regression_test, proposed_fix


def test_question_does_not_name_an_earlier_already_green_edit(git_repo: Path):
    config, worktree, changeset2, regression_test, proposed_fix = (
        _setup_two_edits_no_commit_between(git_repo)
    )

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset2,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[proposed_fix]),
    )

    assert outcome.question is not None, (
        "average()'s change still looks like a revert-able intentional change "
        f"and must produce a question; got {outcome}"
    )
    assert "median" not in outcome.question.message, (
        "the question must name only average() -- the edit that actually broke "
        f"the test -- not median(), finished and unchanged since its own green "
        f"run: {outcome.question.message!r}"
    )
    assert "average" in outcome.question.message


def test_keep_old_behaviour_option_does_not_touch_the_earlier_green_edit(git_repo: Path):
    config, worktree, changeset2, regression_test, proposed_fix = (
        _setup_two_edits_no_commit_between(git_repo)
    )

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset2,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[proposed_fix]),
    )

    assert outcome.question is not None
    keep_old_diff = outcome.question.keep_old_behavior_path.read_text()
    removed_lines = [
        line
        for line in keep_old_diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    assert not any("median" in line or "sorted(values)" in line for line in removed_lines), (
        "the keep-old-behaviour patch must not remove any line from the "
        f"already-green median(); got removed lines:\n{removed_lines}"
    )


def _diff_section(prompt: str) -> str:
    marker = "The user's uncommitted edit, as a unified diff:\n"
    start = prompt.index(marker) + len(marker)
    end = prompt.index("\n\n", start)
    return prompt[start:end]


_FAILING_RAISES_CHECK = (
    CheckResult(
        name="pytest-targeted",
        passed=False,
        output=(
            "FAILED tests/test_average.py::test_average_of_empty_list_raises "
            "- Failed: DID NOT RAISE <class 'ZeroDivisionError'>"
        ),
    ),
)


def test_fix_prompt_diff_section_excludes_the_earlier_already_green_edit(git_repo: Path):
    config, worktree, changeset2, _regression_test, _proposed_fix = (
        _setup_two_edits_no_commit_between(git_repo)
    )

    prompt = build_fix_prompt(
        changeset2, _FAILING_RAISES_CHECK, worktree.path, config.project_root
    )

    assert "median" not in _diff_section(prompt), (
        "the fix prompt's diff section must show only average()'s edit, not "
        f"the already-green median() from an earlier, uncommitted edit:\n{prompt}"
    )


def test_test_prompt_diff_section_excludes_the_earlier_already_green_edit(git_repo: Path):
    config, worktree, changeset2, _regression_test, _proposed_fix = (
        _setup_two_edits_no_commit_between(git_repo)
    )

    prompt = build_test_prompt(
        changeset2, _FAILING_RAISES_CHECK, worktree.path, config.project_root
    )

    assert "median" not in _diff_section(prompt), (
        "the regression-test prompt's diff section must show only average()'s "
        f"edit, not the already-green median() from an earlier, uncommitted edit:\n{prompt}"
    )
