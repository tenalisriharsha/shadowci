from pathlib import Path

from shadowci.checks.pytest_runner import PytestRunner
from shadowci.checks.suite import build_check_runners, run_checks
from shadowci.checks.targeted import TargetedTestRunner
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent


def _changeset(*paths: Path) -> ChangeSet:
    return ChangeSet(
        events=tuple(FileChangeEvent(path=p, kind=ChangeKind.MODIFIED) for p in paths)
    )


def test_uses_targeted_runner_when_a_test_matches(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_foo():\n    assert True\n")

    changeset = _changeset(tmp_path / "src" / "foo.py")
    runners = build_check_runners(changeset, tmp_path)

    assert isinstance(runners[0], TargetedTestRunner)
    assert [runner.name for runner in runners] == ["pytest-targeted", "mypy", "ruff"]


def test_falls_back_to_full_pytest_when_nothing_matches(tmp_path: Path):
    changeset = _changeset(tmp_path / "src" / "foo.py")
    runners = build_check_runners(changeset, tmp_path)

    assert isinstance(runners[0], PytestRunner)
    assert [runner.name for runner in runners] == ["pytest", "mypy", "ruff"]


def test_run_checks_runs_every_runner_in_order(tmp_path: Path):
    changeset = _changeset(tmp_path / "src" / "foo.py")
    runners = build_check_runners(changeset, tmp_path)

    results = run_checks(runners, tmp_path)

    assert [result.name for result in results] == ["pytest", "mypy", "ruff"]
    assert all(result.passed for result in results)
