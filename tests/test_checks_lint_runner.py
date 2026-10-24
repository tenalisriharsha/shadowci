from pathlib import Path

import shadowci.checks.lint_runner as lint_runner
from shadowci.checks.lint_runner import RuffRunner


def test_skips_when_ruff_not_installed(tmp_path: Path):
    result = RuffRunner().run(tmp_path)

    assert result.name == "ruff"
    assert result.passed is True
    assert "not installed" in result.output


def test_passes_when_ruff_reports_no_issues(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(lint_runner, "run_subprocess", lambda args, cwd: (0, "", 0.1))

    result = RuffRunner().run(tmp_path)

    assert result.passed is True


def test_fails_when_ruff_reports_issues(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        lint_runner,
        "run_subprocess",
        lambda args, cwd: (1, "app.py:1:1: F401 unused import\n", 0.1),
    )

    result = RuffRunner().run(tmp_path)

    assert result.passed is False
    assert "F401" in result.output
