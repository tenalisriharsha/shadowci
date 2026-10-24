from pathlib import Path

from shadowci.checks.targeted import TargetedTestRunner, find_targeted_tests
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent


def _changeset(*paths: Path) -> ChangeSet:
    return ChangeSet(
        events=tuple(FileChangeEvent(path=p, kind=ChangeKind.MODIFIED) for p in paths)
    )


def test_matches_by_name_convention(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_foo():\n    assert True\n")
    (tmp_path / "tests" / "test_bar.py").write_text("def test_bar():\n    assert True\n")

    changeset = _changeset(tmp_path / "src" / "pkg" / "foo.py")

    matched = find_targeted_tests(changeset, tmp_path)

    assert matched == [tmp_path / "tests" / "test_foo.py"]


def test_matches_by_reference_when_name_differs(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_integration.py").write_text(
        "from pkg import widget\n\n\ndef test_widget_behavior():\n    assert widget.run()\n"
    )

    changeset = _changeset(tmp_path / "src" / "pkg" / "widget.py")

    matched = find_targeted_tests(changeset, tmp_path)

    assert matched == [tmp_path / "tests" / "test_integration.py"]


def test_no_match_returns_empty_list(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_unrelated.py").write_text("def test_unrelated():\n    assert True\n")

    changeset = _changeset(tmp_path / "src" / "pkg" / "foo.py")

    assert find_targeted_tests(changeset, tmp_path) == []


def test_missing_tests_dir_returns_empty_list(tmp_path: Path):
    changeset = _changeset(tmp_path / "src" / "pkg" / "foo.py")

    assert find_targeted_tests(changeset, tmp_path) == []


def test_targeted_runner_runs_only_matched_tests(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_foo.py").write_text("def test_foo():\n    assert True\n")
    (tmp_path / "tests" / "test_bar.py").write_text("def test_bar():\n    assert False\n")

    changeset = _changeset(tmp_path / "src" / "foo.py")
    runner = TargetedTestRunner(changeset)

    result = runner.run(tmp_path)

    assert result.name == "pytest-targeted"
    assert result.passed is True
    assert "test_bar" not in result.output


def test_targeted_runner_reports_no_match(tmp_path: Path):
    changeset = _changeset(tmp_path / "src" / "foo.py")
    runner = TargetedTestRunner(changeset)

    result = runner.run(tmp_path)

    assert result.passed is True
    assert "no targeted tests matched" in result.output
