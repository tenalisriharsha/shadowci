"""Regression tests for issue #49: the two-option question names the
*file* instead of the changed function and the failing test.

`_save_question` in `pipeline.py` builds its message from
`changeset.paths[0].stem` (the changed file's name) and the update-test
patch's target file's stem (the test *file's* name), and blindly appends
`()` to the former as if it were a function. When the file's stem doesn't
happen to match the function the user actually edited — or the test
file's stem doesn't match the test function that failed — the question
is nonsense, e.g. "Your change to calc() broke test_calc" when the user
only touched `average()` inside `calc.py` and the test that failed is
`test_average_of_nothing_raises`.

These tests pin down that the message instead uses:
- the name(s) of the function(s) whose *body* the user edited (from the
  diff's hunk headers / changed `def` lines), falling back to the file
  name (no `()`) when no function can be identified (a module-level
  edit);
- the name(s) of the failing test *function(s)* from pytest's output,
  not the test file's name;
- "and N more" once there are more than three of either.
"""

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
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
# Scenario 1: the exact case from the issue. The module file is "calc.py"
# but the function the user edits is `average`, and the test file is
# "tests/test_calc.py" but the test function that fails is
# `test_average_of_nothing_raises`. Neither file stem matches the real
# name, so a naive "use the file stem" implementation gets both wrong.
# ---------------------------------------------------------------------------

_MODULE_RELPATH = "calc.py"
_TEST_RELPATH = "tests/test_calc.py"

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

_TEST_CONTENT = (
    "import pytest\n\n"
    "from calc import average\n\n\n"
    "def test_average_of_nothing_raises():\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        average([])\n"
)

_PROPOSED_FIX = _make_diff(_MODULE_RELPATH, _CHANGED_MODULE, _CLEAN_MODULE)


def _run_pipeline(git_repo: Path):
    _write(git_repo, _MODULE_RELPATH, _CLEAN_MODULE)
    _write(git_repo, _TEST_RELPATH, _TEST_CONTENT)
    _commit_all(git_repo, "add average() and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, _MODULE_RELPATH, _CHANGED_MODULE)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_TEST_CONTENT], fixes=[_PROPOSED_FIX]),
    )
    return outcome, config


def test_question_names_the_real_function_and_test_not_the_file_stems(git_repo: Path):
    outcome, _config = _run_pipeline(git_repo)

    assert outcome.question is not None, "expected a two-option question for this change"
    message = outcome.question.message

    assert "calc()" not in message, (
        f"message must not treat the file stem 'calc' as a function name: {message!r}"
    )
    assert "test_calc" not in message.replace("test_calc.py", ""), (
        f"message must not use the test file's stem as the failing test's name: {message!r}"
    )
    assert "average()" in message, f"expected the real changed function average(): {message!r}"
    assert "test_average_of_nothing_raises" in message, (
        f"expected the real failing test name: {message!r}"
    )


def test_question_message_matches_expected_wording_exactly(git_repo: Path):
    outcome, _config = _run_pipeline(git_repo)

    assert outcome.question is not None
    expected = (
        "Your change to average() broke test_average_of_nothing_raises. "
        "Update the test, or keep the old behavior?"
    )
    assert outcome.question.message == expected


# ---------------------------------------------------------------------------
# Scenario 2: a module-level edit — no function body changed (a new
# top-level statement was added) — must fall back to the file name with
# no trailing "()".
# ---------------------------------------------------------------------------

_MODULE_LEVEL_RELPATH = "settings.py"
_MODULE_LEVEL_TEST_RELPATH = "tests/test_settings.py"

# Only the module-level `THRESHOLD = ...` line changes; `check`'s own body
# is untouched. The diff therefore has no changed `def` line to name.
_CLEAN_SETTINGS = (
    "THRESHOLD = 100\n\n\n"
    "def check(value):\n"
    "    if value < THRESHOLD:\n"
    "        raise ValueError('too low')\n"
    "    return value\n"
)
_CHANGED_SETTINGS = (
    "THRESHOLD = 0\n\n\n"
    "def check(value):\n"
    "    if value < THRESHOLD:\n"
    "        raise ValueError('too low')\n"
    "    return value\n"
)

_SETTINGS_TEST = (
    "import pytest\n\n"
    "from settings import check\n\n\n"
    "def test_check_raises_for_low_value():\n"
    "    with pytest.raises(ValueError):\n"
    "        check(50)\n"
)

_SETTINGS_PROPOSED_FIX = _make_diff(
    _MODULE_LEVEL_RELPATH, _CHANGED_SETTINGS, _CLEAN_SETTINGS
)


def test_module_level_edit_falls_back_to_file_name_without_parens(git_repo: Path):
    _write(git_repo, _MODULE_LEVEL_RELPATH, _CLEAN_SETTINGS)
    _write(git_repo, _MODULE_LEVEL_TEST_RELPATH, _SETTINGS_TEST)
    _commit_all(git_repo, "add settings and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, _MODULE_LEVEL_RELPATH, _CHANGED_SETTINGS)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(
            tests=[_SETTINGS_TEST], fixes=[_SETTINGS_PROPOSED_FIX]
        ),
    )

    assert outcome.question is not None
    message = outcome.question.message
    assert "settings()" not in message, (
        f"a module-level edit (no function changed) must not get a fake '()': {message!r}"
    )
    assert "settings" in message, f"expected the file name as fallback: {message!r}"


# ---------------------------------------------------------------------------
# Scenario 3: several functions changed, several tests broken — both lists
# must be named (acceptance test 2), and once there are more than three of
# either, the overflow is summarized as "and N more" rather than silently
# dropped or crashing (acceptance tests 1/4).
# ---------------------------------------------------------------------------


def _guard_fn(name: str, raises: bool) -> str:
    body = "        raise ValueError('too low')\n" if raises else "        pass\n"
    return f"def {name}(value):\n    if value < 0:\n{body}    return value\n"


_MANY_FN_NAMES = ["guard_one", "guard_two", "guard_three", "guard_four", "guard_five"]
_MANY_RELPATH = "guards.py"
_MANY_TEST_RELPATH = "tests/test_guards.py"


def _run_many_pipeline(git_repo: Path, fn_names: list[str]):
    clean = "".join(_guard_fn(name, raises=True) + "\n" for name in fn_names)
    changed = "".join(_guard_fn(name, raises=False) + "\n" for name in fn_names)
    test_content = (
        "import pytest\n\n"
        f"from guards import {', '.join(sorted(fn_names))}\n\n\n"
        + "\n".join(
            f"def test_{name}_raises_for_negative():\n"
            "    with pytest.raises(ValueError):\n"
            f"        {name}(-1)\n"
            for name in fn_names
        )
    )
    proposed_fix = _make_diff(_MANY_RELPATH, changed, clean)

    _write(git_repo, _MANY_RELPATH, clean)
    _write(git_repo, _MANY_TEST_RELPATH, test_content)
    _commit_all(git_repo, "add guard functions and their tests")

    config = _config(git_repo)
    module_path = _write(git_repo, _MANY_RELPATH, changed)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[test_content], fixes=[proposed_fix]),
    )
    return outcome, config


def test_two_changed_functions_and_two_failing_tests_are_both_named(git_repo: Path):
    fn_names = _MANY_FN_NAMES[:2]
    outcome, _config = _run_many_pipeline(git_repo, fn_names)

    assert outcome.question is not None
    message = outcome.question.message

    for name in fn_names:
        assert f"{name}()" in message, f"expected {name}() to be named: {message!r}"
        assert f"test_{name}_raises_for_negative" in message, (
            f"expected test_{name}_raises_for_negative to be named: {message!r}"
        )
    assert "and 1 more" not in message and "and 0 more" not in message, (
        f"only two of each — no overflow should be summarized: {message!r}"
    )


def test_five_failing_tests_name_three_and_summarize_the_rest(git_repo: Path):
    outcome, _config = _run_many_pipeline(git_repo, _MANY_FN_NAMES)

    assert outcome.question is not None
    message = outcome.question.message

    test_names = [f"test_{name}_raises_for_negative" for name in _MANY_FN_NAMES]
    named_count = sum(1 for test_name in test_names if test_name in message)
    assert named_count == 3, (
        f"expected exactly three of the five failing tests to be named, got "
        f"{named_count}: {message!r}"
    )
    assert "and 2 more" in message, (
        f"expected the remaining two failing tests summarized as 'and 2 more': {message!r}"
    )

    fn_named_count = sum(1 for name in _MANY_FN_NAMES if f"{name}()" in message)
    assert fn_named_count == 3, (
        f"expected exactly three of the five changed functions to be named, got "
        f"{fn_named_count}: {message!r}"
    )
