"""Issue #71: a model's fix can delete or weaken existing tests and is
still saved as a verified ghost patch.

Verification only asks "does the full suite pass after the fix?". A fix
that removes or loosens the tests that would catch it — or disables test
collection via conftest.py / pytest.ini — passes that check by
construction. The only guard before this issue is for test files the
*user* edited (issue #38); any other test file is open to the model.

Each scenario below: a real bug in `app.add`, plus a model-proposed fix
that correctly fixes `add` but *also* touches an unrelated, already
passing test (or test infrastructure) so the full suite stays green
either way. Every one of them must be refused before verification, with
no patch saved.
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

_BUGGY_ADD = "def add(a, b):\n    return a + b + 1\n\n\ndef helper():\n    return 42\n"
_FIXED_ADD = "def add(a, b):\n    return a + b\n\n\ndef helper():\n    return 42\n"

_REGRESSION_TEST = (
    "from app import add\n\n\ndef test_shadowci_ghost_regression():\n    assert add(2, 3) == 5\n"
)


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _diff(relative: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    text = "".join(lines)
    return text if text.endswith("\n") else text + "\n"


_APP_FIX = _diff("app.py", _BUGGY_ADD, _FIXED_ADD)


def _delete_file_diff(relative: str, content: str) -> str:
    lines = content.splitlines()
    header = [
        f"diff --git a/{relative} b/{relative}",
        "deleted file mode 100644",
        f"--- a/{relative}",
        "+++ /dev/null",
        f"@@ -1,{len(lines)} +0,0 @@",
    ]
    body = [f"-{line}" for line in lines]
    return "\n".join(header + body) + "\n"


def _new_file_diff(relative: str, content: str) -> str:
    lines = content.splitlines()
    header = [
        f"diff --git a/{relative} b/{relative}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{relative}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    body = [f"+{line}" for line in lines]
    return "\n".join(header + body) + "\n"


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _commit_all(repo: Path, message: str) -> None:
    _run(["add", "-A"], cwd=repo)
    _run(["commit", "-q", "-m", message], cwd=repo)


def _setup_repo(repo: Path, test_app_contents: str, extra_files: dict[str, str] | None = None) -> None:
    """Replaces the `git_repo` fixture's `app.py` with a version that
    already has `add` and `helper`, committed clean, then commits
    `tests/test_app.py` (and any `extra_files`) on top — establishing a
    HEAD where every test already passes, exactly as `shadowci watch`
    would see before the user's buggy edit lands live."""
    (repo / "app.py").write_text(_FIXED_ADD)
    _commit_all(repo, "add helper")

    tests_dir = repo / "tests"
    tests_dir.mkdir(exist_ok=True)
    (tests_dir / "test_app.py").write_text(test_app_contents)
    for relative, content in (extra_files or {}).items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _commit_all(repo, "add tests")


def _sync_buggy_changeset(repo: Path) -> tuple[ShadowWorktree, ChangeSet]:
    live_file = repo / "app.py"
    live_file.write_text(_BUGGY_ADD)
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(_config(repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def _run_pipeline(repo: Path, fix_diff: str) -> pipeline.PipelineOutcome:
    worktree, changeset = _sync_buggy_changeset(repo)
    config = _config(repo)
    return pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_REGRESSION_TEST], fixes=[fix_diff]),
    )


_TEST_APP_WITH_HELPER = (
    "from app import add, helper\n\n\n"
    "def test_app():\n    assert add(2, 3) == 5\n\n\n"
    "def test_helper():\n    assert helper() == 42\n"
)

_TEST_APP_WITH_HELPER_NO_HELPER_TEST = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"

_TEST_APP_WEAKENED = (
    "from app import add, helper\n\n\n"
    "def test_app():\n    assert add(2, 3) == 5\n\n\n"
    "def test_helper():\n    assert helper() is not None\n"
)

_TEST_APP_WITH_PYTEST_IMPORT = (
    "import pytest\n\n"
    "from app import add, helper\n\n\n"
    "def test_app():\n    assert add(2, 3) == 5\n\n\n"
    "def test_helper():\n    assert helper() == 42\n"
)

_TEST_APP_SKIPPED = (
    "import pytest\n\n"
    "from app import add, helper\n\n\n"
    "def test_app():\n    assert add(2, 3) == 5\n\n\n"
    '@pytest.mark.skip(reason="not needed")\n'
    "def test_helper():\n    assert helper() == 42\n"
)


def test_fix_deletes_unrelated_test_is_refused(git_repo: Path):
    _setup_repo(git_repo, _TEST_APP_WITH_HELPER)
    fix_diff = _APP_FIX + _diff(
        "tests/test_app.py", _TEST_APP_WITH_HELPER, _TEST_APP_WITH_HELPER_NO_HELPER_TEST
    )

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that deletes an unrelated test must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "tests/test_app.py" in outcome.ghost_patch_error


def test_fix_weakens_unrelated_assertion_is_refused(git_repo: Path):
    _setup_repo(git_repo, _TEST_APP_WITH_HELPER)
    fix_diff = _APP_FIX + _diff("tests/test_app.py", _TEST_APP_WITH_HELPER, _TEST_APP_WEAKENED)

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that weakens an unrelated test must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "tests/test_app.py" in outcome.ghost_patch_error


def test_fix_adds_skip_marker_to_existing_test_is_refused(git_repo: Path):
    _setup_repo(git_repo, _TEST_APP_WITH_PYTEST_IMPORT)
    fix_diff = _APP_FIX + _diff(
        "tests/test_app.py", _TEST_APP_WITH_PYTEST_IMPORT, _TEST_APP_SKIPPED
    )

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that skips an existing test must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "tests/test_app.py" in outcome.ghost_patch_error


def test_fix_editing_conftest_is_refused(git_repo: Path):
    original_conftest = "# nothing special\n"
    edited_conftest = "# nothing special\n# tweaked by the model\n"
    _setup_repo(
        git_repo,
        _TEST_APP_WITH_HELPER,
        extra_files={"tests/conftest.py": original_conftest},
    )
    fix_diff = _APP_FIX + _diff("tests/conftest.py", original_conftest, edited_conftest)

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that edits conftest.py must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "tests/conftest.py" in outcome.ghost_patch_error


def test_fix_adding_pytest_ini_addopts_is_refused(git_repo: Path):
    original_ini = "[pytest]\n"
    edited_ini = '[pytest]\naddopts = -k "not test_helper"\n'
    _setup_repo(
        git_repo,
        _TEST_APP_WITH_HELPER,
        extra_files={"pytest.ini": original_ini},
    )
    fix_diff = _APP_FIX + _diff("pytest.ini", original_ini, edited_ini)

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that reconfigures pytest.ini must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "pytest.ini" in outcome.ghost_patch_error


def test_fix_deleting_an_existing_test_file_is_refused(git_repo: Path):
    extra_test = "def test_extra():\n    assert True\n"
    _setup_repo(
        git_repo,
        _TEST_APP_WITH_HELPER,
        extra_files={"tests/test_extra.py": extra_test},
    )
    fix_diff = _APP_FIX + _delete_file_diff("tests/test_extra.py", extra_test)

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that deletes an existing test file must not be saved"
    assert outcome.ghost_patch_error is not None
    assert "tests/test_extra.py" in outcome.ghost_patch_error


def test_fix_renaming_an_existing_test_file_is_refused(git_repo: Path):
    extra_test = "def test_extra():\n    assert True\n"
    _setup_repo(
        git_repo,
        _TEST_APP_WITH_HELPER,
        extra_files={"tests/test_extra.py": extra_test},
    )
    fix_diff = (
        _APP_FIX
        + _delete_file_diff("tests/test_extra.py", extra_test)
        + _new_file_diff("tests/test_extra_renamed.py", extra_test)
    )

    outcome = _run_pipeline(git_repo, fix_diff)

    assert outcome.ghost_patch is None, "a fix that renames an existing test file must not be saved"
    assert outcome.ghost_patch_error is not None
