from pathlib import Path

import shadowci.checks.type_checker as type_checker
from shadowci.checks.type_checker import MypyRunner


def test_skips_when_mypy_not_installed(tmp_path: Path):
    result = MypyRunner().run(tmp_path)

    assert result.name == "mypy"
    assert result.passed is True
    assert "not installed" in result.output


def test_passes_when_mypy_reports_no_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type_checker, "run_subprocess", lambda args, cwd: (0, "Success: no issues found\n", 0.1)
    )

    result = MypyRunner().run(tmp_path)

    assert result.passed is True
    assert "Success" in result.output


def test_fails_when_mypy_reports_errors(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        type_checker,
        "run_subprocess",
        lambda args, cwd: (1, "app.py:1: error: Incompatible types\n", 0.1),
    )

    result = MypyRunner().run(tmp_path)

    assert result.passed is False
    assert "Incompatible types" in result.output
