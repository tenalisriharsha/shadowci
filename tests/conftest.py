from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one commit, ready for shadow worktrees."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["init", "-q"], cwd=repo)
    _run(["config", "user.name", "Test User"], cwd=repo)
    _run(["config", "user.email", "test@example.com"], cwd=repo)

    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    _run(["add", "app.py"], cwd=repo)
    _run(["commit", "-q", "-m", "initial commit"], cwd=repo)

    return repo
