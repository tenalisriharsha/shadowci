"""Regression tests for issue #48: the "update the test" option must never
leave a test that checks nothing.

`build_update_test_patch` (in `shadowci.revert_check`) rewrites a failing
`with pytest.raises(...):` block to match the user's new, intentional
behavior. Today it just unwraps the block and drops the bare call in its
place — `test_average_of_empty_list_raises` becomes

    def test_average_of_empty_list_raises():
        average([])

which passes no matter what `average([])` returns, because nothing is
asserted. The suite goes green over a test that checks nothing.

The correct rewrite is `assert <call> == <value>`, built from the call's
*real* current result, and only offered when that value round-trips through
`ast.literal_eval(repr(value))` and the block is unambiguous (a single
statement). Otherwise `build_update_test_patch` must return `None` so the
pipeline falls back to its normal flow instead of asking the user to accept
a test that asserts nothing.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.revert_check import build_update_test_patch
from shadowci.shadow_worktree import ShadowWorktree

TEST_RELPATH = "tests/test_calc.py"
MODULE_RELPATH = "calc.py"
TEST_NAME = "test_compute_raises"


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _build_patch(
    repo: Path,
    clean_module: str,
    changed_module: str,
    test_source: str,
    raised_exception: str = "ValueError",
):
    """Commit `clean_module`/`test_source`, then apply `changed_module` as
    the user's uncommitted edit, sync a shadow worktree and call
    `build_update_test_patch` exactly as the pipeline would."""
    (repo / MODULE_RELPATH).write_text(clean_module)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / TEST_RELPATH).write_text(test_source)
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", "add calc and its test"], repo)

    module_path = repo / MODULE_RELPATH
    module_path.write_text(changed_module)

    config = ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))
    worktree = ShadowWorktree(config)
    events = [FileChangeEvent(path=module_path, kind=ChangeKind.MODIFIED)]
    worktree.sync(events)
    changeset = ChangeSet(events=tuple(events))

    originally_failing = (
        CheckResult(
            name="pytest",
            passed=False,
            output=(
                f"FAILED {TEST_RELPATH}::{TEST_NAME} - "
                f"DID NOT RAISE <class '{raised_exception}'>"
            ),
        ),
    )
    return build_update_test_patch(worktree, changeset, originally_failing)


def _test_source(exception_name: str) -> str:
    return (
        "import pytest\n\n"
        "from calc import compute\n\n\n"
        f"def {TEST_NAME}():\n"
        f"    with pytest.raises({exception_name}):\n"
        "        compute()\n"
    )


def _added_lines(diff_text: str, relpath: str) -> list[str]:
    """The `+` lines (without the leading `+`) belonging to `relpath`'s
    hunks in a unified diff produced by `difflib.unified_diff`."""
    lines: list[str] = []
    in_file = False
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            in_file = line == f"+++ b/{relpath}"
            continue
        if line.startswith("--- "):
            continue
        if in_file and line.startswith("+"):
            lines.append(line[1:])
    return lines


def _assert_comparator(added_lines: list[str]) -> ast.expr:
    """The right-hand side of the single `assert <call> == <value>` line
    among `added_lines`, parsed. Fails the test (not returns None) when
    there isn't exactly one such line — the whole point of this issue is
    that a rewrite without an assertion must not happen."""
    candidates = [line for line in added_lines if "assert" in line]
    assert len(candidates) == 1, (
        f"expected exactly one added assert line, got added_lines={added_lines!r}"
    )
    tree = ast.parse(candidates[0].strip())
    (stmt,) = tree.body
    assert isinstance(stmt, ast.Assert), f"added line is not an assert: {candidates[0]!r}"
    test = stmt.test
    assert isinstance(test, ast.Compare) and len(test.comparators) == 1, (
        f"expected a single `== value` comparison, got: {candidates[0]!r}"
    )
    return test.comparators[0]


def test_update_test_rewrite_produces_an_assertion_not_a_bare_call(git_repo: Path):
    """The exact scenario from the issue: average([]) stops raising and
    starts returning 0. The rewritten test must assert that 0, not just
    call average([]) and discard the result."""
    clean = (
        "def average(values):\n"
        "    total = sum(values)\n"
        "    return total / len(values)\n"
    )
    changed = (
        "def average(values):\n"
        "    if not values:\n"
        "        return 0\n"
        "    total = sum(values)\n"
        "    return total / len(values)\n"
    )
    test_source = (
        "import pytest\n\n"
        "from average import average\n\n\n"
        "def test_average_of_empty_list_raises():\n"
        "    with pytest.raises(ZeroDivisionError):\n"
        "        average([])\n"
    )
    (git_repo / "average.py").write_text(clean)
    (git_repo / "tests").mkdir(exist_ok=True)
    (git_repo / "tests" / "test_average.py").write_text(test_source)
    _run_git(["add", "-A"], git_repo)
    _run_git(["commit", "-q", "-m", "add average and its test"], git_repo)

    module_path = git_repo / "average.py"
    module_path.write_text(changed)

    config = ShadowCIConfig(project_root=git_repo, shadow_dir=Path(".shadowci/shadow"))
    worktree = ShadowWorktree(config)
    events = [FileChangeEvent(path=module_path, kind=ChangeKind.MODIFIED)]
    worktree.sync(events)
    changeset = ChangeSet(events=tuple(events))
    originally_failing = (
        CheckResult(
            name="pytest",
            passed=False,
            output="FAILED tests/test_average.py::test_average_of_empty_list_raises - DID NOT RAISE",
        ),
    )

    patch = build_update_test_patch(worktree, changeset, originally_failing)

    assert patch is not None, "expected a verified update-test patch, got None"
    added = _added_lines(patch.diff, "tests/test_average.py")
    comparator = _assert_comparator(added)
    assert ast.literal_eval(comparator) == 0

    no_bare_call = not any(
        line.strip() == "average([])" for line in added
    )
    assert no_bare_call, (
        f"rewritten test still contains a bare, un-asserted call: added_lines={added!r}"
    )


def test_update_test_rewrite_round_trips_a_float_result(git_repo: Path):
    clean = "def compute():\n    raise ValueError('nope')\n"
    changed = "def compute():\n    return 0.1 + 0.2\n"
    patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))

    assert patch is not None
    added = _added_lines(patch.diff, TEST_RELPATH)
    comparator = _assert_comparator(added)
    assert ast.literal_eval(comparator) == 0.1 + 0.2


def test_update_test_rewrite_round_trips_a_list_result(git_repo: Path):
    clean = "def compute():\n    raise ValueError('nope')\n"
    changed = "def compute():\n    return [1, 2, 3]\n"
    patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))

    assert patch is not None
    added = _added_lines(patch.diff, TEST_RELPATH)
    comparator = _assert_comparator(added)
    assert ast.literal_eval(comparator) == [1, 2, 3]


def test_update_test_rewrite_round_trips_a_string_result(git_repo: Path):
    clean = "def compute():\n    raise ValueError('nope')\n"
    changed = "def compute():\n    return \"it's fine now\"\n"
    patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))

    assert patch is not None
    added = _added_lines(patch.diff, TEST_RELPATH)
    comparator = _assert_comparator(added)
    assert ast.literal_eval(comparator) == "it's fine now"


def test_update_test_option_not_offered_for_a_non_literal_result(git_repo: Path):
    """compute() now returns an arbitrary object with no literal form
    (default `object()` repr is a memory address, not round-trippable via
    `ast.literal_eval`) — there is no deterministic way to write the
    assertion, so no "update the test" option must be produced at all."""
    clean = "def compute():\n    raise ValueError('nope')\n"
    changed = "def compute():\n    return object()\n"
    patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))

    assert patch is None


def test_update_test_option_not_offered_when_the_call_now_raises_a_different_exception(
    git_repo: Path,
):
    """compute() still raises, just not the exception the old test
    expected — rewriting to an assertion would be wrong (there's no return
    value), so no option must be produced."""
    clean = "def compute():\n    raise ValueError('nope')\n"
    changed = "def compute():\n    raise TypeError('still broken, differently')\n"
    patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))

    assert patch is None


def test_update_test_option_not_offered_for_an_ambiguous_multi_statement_block(git_repo: Path):
    """Several statements inside the `with pytest.raises(...):` block make
    "the result" ambiguous (which statement's value would the test even
    assert?) — no option must be produced, and in particular the rewrite
    must never leave the two statements bare with no assertion at all."""
    clean = "def compute():\n    raise ValueError('nope')\n\n\ndef touch():\n    pass\n"
    changed = "def compute():\n    return 42\n\n\ndef touch():\n    pass\n"
    test_source = (
        "import pytest\n\n"
        "from calc import compute, touch\n\n\n"
        f"def {TEST_NAME}():\n"
        "    with pytest.raises(ValueError):\n"
        "        touch()\n"
        "        compute()\n"
    )
    patch = _build_patch(git_repo, clean, changed, test_source)

    assert patch is None


def test_update_test_rewrite_never_leaves_a_function_without_an_assertion(git_repo: Path):
    """Property check (acceptance #5): across every kind of literal result
    this issue calls out, the rewritten test function must contain an
    assertion — never a bare, un-asserted call."""
    cases = {
        "zero": (0, "def compute():\n    return 0\n"),
        "negative_int": (-7, "def compute():\n    return -7\n"),
        "bool_true": (True, "def compute():\n    return True\n"),
        "none": (None, "def compute():\n    return None\n"),
        "bytes": (b"abc", "def compute():\n    return b'abc'\n"),
        "tuple": ((1, "x"), "def compute():\n    return (1, 'x')\n"),
        "dict": ({"a": 1}, "def compute():\n    return {'a': 1}\n"),
        "set": ({1, 2}, "def compute():\n    return {1, 2}\n"),
    }
    clean = "def compute():\n    raise ValueError('nope')\n"
    for expected, changed in cases.values():
        patch = _build_patch(git_repo, clean, changed, _test_source("ValueError"))
        assert patch is not None, f"expected a patch for {changed!r}"
        added = _added_lines(patch.diff, TEST_RELPATH)
        comparator = _assert_comparator(added)
        assert ast.literal_eval(comparator) == expected
