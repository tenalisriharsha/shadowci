"""Regression tests for issue #50: the "keep old behaviour" option of the
two-option question (issue #37) adds a brand-new regression test file under
`tests/` even when an existing test already covers exactly that behavior.

Observed in the wild: `average([])` was changed to return 0 instead of
raising, breaking the existing `test_average_of_nothing_raises`. The
"keep old behaviour" option restores `average()`'s old body — the already
failing `test_average_of_nothing_raises` *is* the regression test for that
revert, since reverting the source makes it pass again. But
`run_checks_and_maybe_escalate` builds the "keep old behaviour" patch with
`GhostPatchBuilder.build(...)`, which unconditionally writes the model's
proposed `regression_test` into a brand-new
`tests/test_shadowci_ghost_<digest>.py` file and bundles it into the diff —
even when that test's content (or an equivalent one) already exists on
disk. Applying the patch then leaves two tests asserting the same thing.

These tests pin down that for the "keep old behaviour" option specifically:

- its saved diff touches only the reverted source file, never a new file
  under `tests/` (acceptance test 1);
- applying it makes the previously failing existing test pass, with no new
  ghost test file appearing on disk (acceptance test 2);
- a normal verified fix (the garbage-code / crash scenario, which never
  goes through the question flow) still bundles its regression test file as
  before (acceptance test 3);
- applying either question option still records exactly one accept signal
  and retires its sibling (acceptance test 4).
"""

from __future__ import annotations

import difflib
import re
import subprocess
import sys
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.evaluation.scenarios import garbage_code_scenario, intentional_behavior_change_scenario
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree


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


def _sync(config: ShadowCIConfig, path: Path) -> tuple[ShadowWorktree, ChangeSet]:
    changeset = ChangeSet(events=(FileChangeEvent(path=path, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(config)
    worktree.sync(list(changeset.events))
    return worktree, changeset


def _diff_target_files(diff_text: str) -> set[str]:
    return set(re.findall(r"^\+\+\+ b/(.+)$", diff_text, re.MULTILINE))


def _question_listings(root: Path) -> list:
    return [
        listing
        for listing in pipeline.list_ghost_patches(root)
        if (listing.metadata or {}).get("kind") == "question"
    ]


def _make_diff(relpath: str, before: str, after: str) -> str:
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relpath}",
        tofile=f"b/{relpath}",
    )
    text = "".join(diff_lines)
    return text if text.endswith("\n") else text + "\n"


# ---------------------------------------------------------------------------
# The exact scenario from the issue: average([]) is changed to return 0 on
# purpose instead of raising, breaking the existing test_average_of_nothing
# -raises. The "keep old behaviour" option reverts average.py — the already
# -failing existing test covers that revert, so the patch must not bundle a
# fresh copy of it under a new ghost-test filename.
# ---------------------------------------------------------------------------

_MODULE_RELPATH = "average.py"
_TEST_RELPATH = "tests/test_average.py"

_CLEAN_MODULE = (
    "def average(values):\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)

_CHANGED_MODULE = (
    "def average(values):\n"
    "    if not values:\n"
    "        return 0\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)

_EXISTING_TEST = (
    "import pytest\n\n"
    "from average import average\n\n\n"
    "def test_average_of_nothing_raises():\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        average([])\n"
)

_PROPOSED_FIX = _make_diff(_MODULE_RELPATH, _CHANGED_MODULE, _CLEAN_MODULE)


def _run_pipeline(git_repo: Path):
    _write(git_repo, _MODULE_RELPATH, _CLEAN_MODULE)
    _write(git_repo, _TEST_RELPATH, _EXISTING_TEST)
    _commit_all(git_repo, "add average() and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, _MODULE_RELPATH, _CHANGED_MODULE)
    worktree, changeset = _sync(config, module_path)

    # The provider's "regression test" reply is exactly the already-existing
    # test — the realistic case the issue describes, where the model simply
    # echoes back the test that is already failing.
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_EXISTING_TEST], fixes=[_PROPOSED_FIX]),
    )
    return outcome, config


def test_keep_old_behavior_patch_touches_only_the_reverted_source_file(git_repo: Path):
    outcome, config = _run_pipeline(git_repo)
    assert outcome.question is not None, "expected a two-option question for this change"

    keep_old_diff = outcome.question.keep_old_behavior_path.read_text()
    targets = _diff_target_files(keep_old_diff)

    assert targets == {_MODULE_RELPATH}, (
        f"'keep old behaviour' patch must touch only {_MODULE_RELPATH}, "
        f"not add a new test file: touched {targets}"
    )
    assert "new file mode" not in keep_old_diff, (
        f"'keep old behaviour' patch must not create any new file:\n{keep_old_diff}"
    )


def test_applying_keep_old_behavior_fixes_the_existing_test_with_no_new_ghost_test(
    git_repo: Path,
):
    outcome, config = _run_pipeline(git_repo)
    root = config.project_root
    assert outcome.question is not None

    ghost_dir = root / ".shadowci" / "ghost"
    before_test_files = {path.name for path in (root / "tests").glob("*.py")}

    result = pipeline.accept_patch(root, outcome.question.keep_old_behavior_path)
    assert result.ok, result.message

    after_test_files = {path.name for path in (root / "tests").glob("*.py")}
    assert after_test_files == before_test_files, (
        f"applying 'keep old behaviour' must not add any file under tests/: "
        f"before={before_test_files} after={after_test_files}"
    )
    assert not any(ghost_dir.glob("test_shadowci_ghost_*.py")), (
        "no ghost regression-test file should exist for the 'keep old behaviour' option"
    )

    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_average.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert suite.returncode == 0, (
        "the previously failing existing test must pass after the revert is applied:\n"
        + suite.stdout
        + suite.stderr
    )


# ---------------------------------------------------------------------------
# Acceptance test 3: an ordinary verified fix (the crash/garbage-code
# scenario, which never goes through the question flow at all) must still
# bundle its own regression test file exactly as before.
# ---------------------------------------------------------------------------


def test_normal_verified_fix_still_adds_its_regression_test(git_repo: Path):
    scenario = garbage_code_scenario()
    edit = scenario.session.edits[0]
    module_relpath = edit.relative_path
    clean_content = scenario.session.initial_files[module_relpath]
    buggy_content = edit.content
    test_relpath = next(
        path for path in scenario.session.initial_files if path != module_relpath
    )
    regression_test = scenario.regression_test
    proposed_fix = scenario.proposed_fix
    assert buggy_content is not None
    assert regression_test is not None
    assert proposed_fix is not None

    _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add calc.add and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, module_relpath, buggy_content)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[proposed_fix]),
    )

    assert outcome.ghost_patch is not None, outcome.ghost_patch_error
    targets = _diff_target_files(outcome.ghost_patch.diff)
    ghost_test_targets = {t for t in targets if re.match(r"tests/test_shadowci_ghost_.*\.py$", t)}
    assert ghost_test_targets, (
        f"a normal verified fix must still bundle its regression test file, got targets={targets}"
    )
    assert "new file mode" in outcome.ghost_patch.diff


# ---------------------------------------------------------------------------
# Acceptance test 4: applying either question option still records exactly
# one accept signal and retires its sibling.
# ---------------------------------------------------------------------------


def test_applying_keep_old_behavior_records_one_signal_and_retires_sibling(git_repo: Path):
    outcome, config = _run_pipeline(git_repo)
    root = config.project_root
    assert outcome.question is not None

    sibling_path = outcome.question.update_test_path
    assert sibling_path.is_file()

    result = pipeline.accept_patch(root, outcome.question.keep_old_behavior_path)
    assert result.ok, result.message

    assert not sibling_path.exists()
    assert not pipeline.metadata_path(sibling_path).exists()

    store = pipeline.signal_store(root)
    assert len(store.all()) == 1


def test_applying_update_test_option_records_one_signal_and_retires_sibling(git_repo: Path):
    outcome, config = _run_pipeline(git_repo)
    root = config.project_root
    assert outcome.question is not None

    sibling_path = outcome.question.keep_old_behavior_path
    assert sibling_path.is_file()

    result = pipeline.accept_patch(root, outcome.question.update_test_path)
    assert result.ok, result.message

    assert not sibling_path.exists()
    assert not pipeline.metadata_path(sibling_path).exists()

    store = pipeline.signal_store(root)
    assert len(store.all()) == 1
