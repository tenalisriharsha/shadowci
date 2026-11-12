"""Regression tests for issue #53's `shadowci doctor` check: a shadow
worktree whose `HEAD` has drifted from the live tree's `HEAD` (the user
committed, pulled, or switched branches without the shadow ever moving)
must be diagnosed, and `doctor --fix` must repair it by rebaselining —
not just by recreating a missing worktree, which `doctor` already does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci.cli import main
from shadowci.config import load_config
from shadowci.shadow_worktree import ShadowWorktree


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _head_sha(cwd: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _make_shadow_stale(git_repo: Path) -> None:
    """Move live `HEAD` without touching the (already-created) shadow."""
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # changed after init\n")
    _commit_all(git_repo, "change add() after shadow creation")


def test_doctor_warns_when_the_shadow_head_has_drifted_from_live_head(
    git_repo: Path, capsys
):
    main(["init", str(git_repo)])
    capsys.readouterr()

    _make_shadow_stale(git_repo)

    main(["doctor", str(git_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "WARN" in combined, combined
    assert "head" in combined.lower()
    assert "Try:" in combined


def test_doctor_does_not_warn_when_the_shadow_head_matches_live_head(
    git_repo: Path, capsys
):
    main(["init", str(git_repo)])
    out = capsys.readouterr().out

    assert "stale" not in out.lower()


def test_doctor_fix_rebaselines_a_stale_shadow_head(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    _make_shadow_stale(git_repo)

    main(["doctor", str(git_repo), "--fix"])
    capsys.readouterr()

    config = load_config(git_repo)
    worktree = ShadowWorktree(config)

    assert _head_sha(worktree.path) == _head_sha(git_repo)

    exit_code_after = main(["doctor", str(git_repo)])
    out_after = capsys.readouterr().out

    assert "FAIL" not in out_after
    assert "WARN" not in out_after or "head" not in out_after.lower()
    assert exit_code_after == 0
