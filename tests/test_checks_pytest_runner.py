from pathlib import Path

from shadowci.checks.pytest_runner import PytestRunner


def test_passes_when_all_tests_pass(tmp_path: Path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")

    result = PytestRunner().run(tmp_path)

    assert result.name == "pytest"
    assert result.passed is True


def test_fails_when_a_test_fails(tmp_path: Path):
    (tmp_path / "test_bad.py").write_text("def test_bad():\n    assert False\n")

    result = PytestRunner().run(tmp_path)

    assert result.passed is False
    assert "test_bad" in result.output


def test_no_tests_collected_counts_as_passed(tmp_path: Path):
    result = PytestRunner().run(tmp_path)

    assert result.passed is True
    assert "no tests collected" in result.output
