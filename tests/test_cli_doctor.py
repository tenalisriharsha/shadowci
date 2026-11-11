"""Regression tests for issue #28: no `doctor` command diagnoses problems.

`shadowci --help` never offered a diagnostic command, so an expired model
login, a missing shadow worktree, or a stale saved patch each had to be
tracked down by hand, with no command telling the user the fix. These
tests exercise the `doctor` command (and `doctor --fix`) described in the
issue's suggested fix, against a real temporary git repository.
"""

from __future__ import annotations

import json
from pathlib import Path

from shadowci.cli import main

GHOST_DIRNAME = Path(".shadowci") / "ghost"

APP_PY = "def add(a, b):\n    return a + b\n"

STALE_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 999\n"
    "+    return a + b\n"
)

READY_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    return a + b  # fixed\n"
)


def _write_ghost_patch(root: Path, digest: str, diff: str, fingerprint: str) -> Path:
    ghost_dir = root / GHOST_DIRNAME
    ghost_dir.mkdir(parents=True, exist_ok=True)
    patch_path = ghost_dir / f"{digest}.patch"
    patch_path.write_text(diff)
    patch_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint,
                "summary": f"patch {digest}",
                "files": ["app.py"],
            }
        )
    )
    return patch_path


def test_help_lists_doctor_command(capsys):
    try:
        main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out

    assert "doctor" in out


def test_doctor_reports_ok_for_every_check_in_a_healthy_project(git_repo: Path, capsys):
    exit_code = main(["init", str(git_repo)])
    assert exit_code == 0
    capsys.readouterr()

    exit_code = main(["doctor", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "FAIL" not in out
    assert "WARN" not in out
    assert out.count("OK") >= 3


def test_doctor_reports_a_failing_provider_with_its_real_message_and_a_try_hint(
    git_repo: Path, capsys, monkeypatch, tmp_path
):
    (git_repo / "shadowci.toml").write_text('[shadowci]\nprovider = "claude-cli"\n')
    exit_code = main(["init", str(git_repo)])
    assert exit_code == 0
    capsys.readouterr()

    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))

    exit_code = main(["doctor", str(git_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "FAIL" in combined
    assert "not found on PATH" in combined
    assert "Try:" in combined


def test_doctor_reports_missing_worktree_and_stale_patch_then_fix_repairs_them(
    git_repo: Path, capsys
):
    (git_repo / "shadowci.toml").write_text('[shadowci]\nprovider = "scripted"\n')
    ready_patch = _write_ghost_patch(git_repo, "readyaaa11", READY_FIX, "fix-ready")
    stale_patch = _write_ghost_patch(git_repo, "staleaaa11", STALE_FIX, "fix-stale")

    assert not (git_repo / ".shadowci" / "shadow").exists()

    exit_code = main(["doctor", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "shadow worktree" in out
    assert "staleaaa11" in out

    exit_code = main(["doctor", str(git_repo), "--fix"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert (git_repo / ".shadowci" / "shadow").is_dir()
    assert (git_repo / ".shadowci" / "shadow" / ".git").exists()

    assert not stale_patch.exists()
    assert not stale_patch.with_suffix(".json").exists()
    assert ready_patch.exists()
    assert ready_patch.with_suffix(".json").exists()

    assert (git_repo / "app.py").read_text() == APP_PY
