"""Issue #39: the model prompts must carry the diff, file contents, and the
failing test's source — not just file paths and check output — and must
offer the model a deliberate way to say a change looks intentional instead
of only ever being told to "return only the diff"."""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci.classification import FailureClass, FailureClassification
from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.providers.prompts import build_fix_prompt, build_test_prompt

_FAILING_CHECKS = (
    CheckResult(
        name="pytest",
        passed=False,
        output="FAILED tests/test_app.py::test_app - assert 6 == 5",
    ),
)


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _changeset(*paths: Path, kind: ChangeKind = ChangeKind.MODIFIED) -> ChangeSet:
    return ChangeSet(events=tuple(FileChangeEvent(path=path, kind=kind) for path in paths))


def test_fix_prompt_includes_diff_file_contents_and_test_source(git_repo: Path):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
    )

    changeset = _changeset(git_repo / "app.py")
    prompt = build_fix_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    # the diff: a unified diff of the user's uncommitted edit, not just the
    # file's current contents
    assert "--- a/app.py" in prompt
    assert "+++ b/app.py" in prompt
    assert "+    return a + b + 1" in prompt

    # the failing test's own source, not just its name
    assert "assert add(2, 3) == 5" in prompt


def test_test_prompt_includes_diff_file_contents_and_test_source(git_repo: Path):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
    )

    changeset = _changeset(git_repo / "app.py")
    prompt = build_test_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    assert "--- a/app.py" in prompt
    assert "+++ b/app.py" in prompt
    assert "+    return a + b + 1" in prompt
    assert "assert add(2, 3) == 5" in prompt


def test_large_changed_file_is_truncated_with_marker(git_repo: Path):
    big = git_repo / "big.py"
    big.write_text("x" * 100_000)

    changeset = _changeset(big, kind=ChangeKind.CREATED)
    prompt = build_fix_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    assert "x" * 100_000 not in prompt
    assert "[truncated]" in prompt


def test_sensitive_files_never_appear_in_a_prompt(git_repo: Path):
    env_file = git_repo / ".env"
    env_file.write_text("SECRET_DB_PASSWORD=old-password\n")
    secret_file = git_repo / "secret_keys.py"
    secret_file.write_text("API_TOKEN = 'old-token'\n")
    _run(["add", ".env", "secret_keys.py"], cwd=git_repo)
    _run(["commit", "-q", "-m", "add sensitive files"], cwd=git_repo)

    env_file.write_text("SECRET_DB_PASSWORD=supersecretvalue123\n")
    secret_file.write_text("API_TOKEN = 'leak-this-token-9999'\n")

    changeset = _changeset(env_file, secret_file)

    fix_prompt = build_fix_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)
    test_prompt = build_test_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    for prompt in (fix_prompt, test_prompt):
        assert "supersecretvalue123" not in prompt
        assert "leak-this-token-9999" not in prompt
        assert "old-password" not in prompt
        assert "old-token" not in prompt


def test_prompt_builds_without_a_diff_for_a_brand_new_file(git_repo: Path):
    new_file = git_repo / "new_module.py"
    new_file.write_text("def greet():\n    return 'hi'\n")

    changeset = _changeset(new_file, kind=ChangeKind.CREATED)

    fix_prompt = build_fix_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)
    test_prompt = build_test_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    assert isinstance(fix_prompt, str) and fix_prompt
    assert isinstance(test_prompt, str) and test_prompt


def test_prompt_includes_failure_class_and_edited_test_files_rule(git_repo: Path):
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    test_file = tests_dir / "test_app.py"
    test_file.write_text("def test_app():\n    assert False\n")

    classification = FailureClassification(
        failure_class=FailureClass.TEST_EDITED,
        edited_test_files=("tests/test_app.py",),
    )
    changeset = _changeset(test_file)

    prompt = build_fix_prompt(
        changeset, _FAILING_CHECKS, git_repo, git_repo, classification=classification
    )

    assert "test-edited" in prompt
    assert "tests/test_app.py" in prompt
    lowered = prompt.lower()
    assert "never" in lowered
    assert "modify" in lowered or "edit" in lowered or "touch" in lowered


def test_fix_prompt_offers_the_intentional_reply_format(git_repo: Path):
    changeset = _changeset(git_repo / "app.py")
    prompt = build_fix_prompt(changeset, _FAILING_CHECKS, git_repo, git_repo)

    assert "INTENTIONAL:" in prompt
