from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.ghost import (
    GhostPatchBuilder,
    GhostPatchError,
    apply_to_live_tree,
    check_applies_to_live_tree,
)
from shadowci.shadow_worktree import ShadowWorktree

_BUGGY_APP = "def add(a, b):\n    return a + b + 1\n"
_FIXED_APP = "def add(a, b):\n    return a + b\n"

_REGRESSION_TEST = (
    "import pathlib\n"
    "\n"
    "def test_add_regression():\n"
    "    source = pathlib.Path('app.py').read_text()\n"
    "    namespace = {}\n"
    "    exec(source, namespace)\n"
    "    assert namespace['add'](2, 3) == 5\n"
)

_VALID_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 1\n"
    "+    return a + b\n"
)

_FAILING_CHECKS = (CheckResult(name="pytest-targeted", passed=False, output="AssertionError"),)


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _sync_buggy_app(git_repo: Path) -> tuple[ShadowWorktree, ChangeSet]:
    live_file = git_repo / "app.py"
    live_file.write_text(_BUGGY_APP)
    changeset = ChangeSet(
        events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),)
    )
    worktree = ShadowWorktree(_config(git_repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def test_build_produces_a_verified_ghost_patch(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)

    patch = GhostPatchBuilder(worktree).build(
        changeset, _FAILING_CHECKS, _REGRESSION_TEST, _VALID_FIX
    )

    assert "return a + b" in patch.diff
    assert "test_add_regression" in patch.diff
    assert patch.evidence.all_checks_passed
    assert patch.evidence.failing_test_before == _REGRESSION_TEST
    assert patch.changeset == changeset


def test_build_rejects_a_test_that_does_not_reproduce_the_bug(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)
    always_passing_test = "def test_noop():\n    assert True\n"

    try:
        GhostPatchBuilder(worktree).build(
            changeset, _FAILING_CHECKS, always_passing_test, _VALID_FIX
        )
        assert False, "expected GhostPatchError"
    except GhostPatchError as exc:
        assert "did not reproduce" in str(exc)

    leftover = list((worktree.path / "tests").glob("test_shadowci_ghost_*.py"))
    assert leftover == []


def test_build_rejects_a_fix_that_does_not_apply(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)
    malformed_fix = "not a real diff"

    try:
        GhostPatchBuilder(worktree).build(
            changeset, _FAILING_CHECKS, _REGRESSION_TEST, malformed_fix
        )
        assert False, "expected GhostPatchError"
    except GhostPatchError as exc:
        assert "did not apply cleanly" in str(exc)

    assert (worktree.path / "app.py").read_text() == _BUGGY_APP
    leftover = list((worktree.path / "tests").glob("test_shadowci_ghost_*.py"))
    assert leftover == []


def test_build_rejects_a_fix_that_regresses_something_else(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)

    (git_repo / "tests").mkdir(exist_ok=True)
    (git_repo / "tests" / "test_unrelated.py").write_text(
        "def test_unrelated():\n    assert False\n"
    )
    worktree.sync(
        [FileChangeEvent(path=git_repo / "tests" / "test_unrelated.py", kind=ChangeKind.CREATED)]
    )

    try:
        GhostPatchBuilder(worktree).build(
            changeset, _FAILING_CHECKS, _REGRESSION_TEST, _VALID_FIX
        )
        assert False, "expected GhostPatchError"
    except GhostPatchError as exc:
        assert "full suite still fails" in str(exc)


def test_apply_to_live_tree_writes_the_fix_and_the_regression_test(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)
    patch = GhostPatchBuilder(worktree).build(
        changeset, _FAILING_CHECKS, _REGRESSION_TEST, _VALID_FIX
    )

    apply_to_live_tree(patch.diff, git_repo)

    assert (git_repo / "app.py").read_text() == _FIXED_APP
    ghost_tests = list((git_repo / "tests").glob("test_shadowci_ghost_*.py"))
    assert len(ghost_tests) == 1
    assert ghost_tests[0].read_text() == _REGRESSION_TEST


def test_apply_to_live_tree_refuses_a_patch_that_no_longer_applies(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)
    patch = GhostPatchBuilder(worktree).build(
        changeset, _FAILING_CHECKS, _REGRESSION_TEST, _VALID_FIX
    )

    (git_repo / "app.py").write_text("def add(a, b):\n    return (a + b) + 1\n")

    assert check_applies_to_live_tree(patch.diff, git_repo) is False
    try:
        apply_to_live_tree(patch.diff, git_repo)
        assert False, "expected GhostPatchError"
    except GhostPatchError as exc:
        assert "no longer applies cleanly" in str(exc)

    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return (a + b) + 1\n"


def test_apply_to_live_tree_tolerates_unrelated_edits_made_meanwhile(git_repo: Path):
    worktree, changeset = _sync_buggy_app(git_repo)
    patch = GhostPatchBuilder(worktree).build(
        changeset, _FAILING_CHECKS, _REGRESSION_TEST, _VALID_FIX
    )

    (git_repo / "unrelated.py").write_text("VALUE = 1\n")

    assert check_applies_to_live_tree(patch.diff, git_repo) is True
    apply_to_live_tree(patch.diff, git_repo)

    assert (git_repo / "app.py").read_text() == _FIXED_APP
    assert (git_repo / "unrelated.py").read_text() == "VALUE = 1\n"
