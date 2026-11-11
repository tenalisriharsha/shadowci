"""Regression tests for issue #28: no `doctor` command diagnoses problems.

`shadowci --help` never listed a diagnostic command — the choices were
`init`, `status`, `watch`, `radar`, `apply-ghost`, `reject-ghost`,
`ignore-ghost`, `ghosts`, `signals`, `evaluate`. Checking whether ShadowCI
can actually work in a project (git repo? config readable? shadow worktree
present? configured provider answers? pytest/mypy/ruff importable? any
saved ghost patch gone stale?) had to be done by hand, file by file. These
tests exercise the `doctor` command (and `doctor --fix`) described in the
issue's suggested fix, against real temporary git repositories.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci.cli import main
from shadowci.config import load_config
from shadowci.shadow_worktree import ShadowWorktree

ORIGINAL_APP_PY = "def add(a, b):\n    return a + b\n"

READY_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b\n"
    "+    return a + b  # fixed\n"
)

STALE_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a - b\n"
    "+    return a - b  # fixed\n"
)


def _write_ghost_patch(root: Path, digest: str, diff: str, fingerprint: str) -> Path:
    ghost_dir = root / ".shadowci" / "ghost"
    ghost_dir.mkdir(parents=True, exist_ok=True)
    patch_path = ghost_dir / f"{digest}.patch"
    patch_path.write_text(diff)
    (ghost_dir / f"{digest}.json").write_text(
        f'{{"digest": "{digest}", "fingerprint": "{fingerprint}", '
        f'"summary": "test fix", "files": ["app.py"]}}'
    )
    return patch_path


def test_doctor_reports_no_failures_in_a_healthy_project(git_repo: Path, capsys):
    exit_code_init = main(["init", str(git_repo)])
    capsys.readouterr()
    assert exit_code_init == 0

    exit_code = main(["doctor", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "FAIL" not in out
    # Checks are actually reported, not a silently empty report.
    assert "OK" in out or "WARN" in out


def test_doctor_reports_fail_when_the_project_is_not_a_git_repo(tmp_path: Path, capsys):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    (not_a_repo / "app.py").write_text(ORIGINAL_APP_PY)

    exit_code = main(["doctor", str(not_a_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "FAIL" in combined
    assert "git" in combined.lower()
    assert "Try:" in combined


def test_doctor_reports_fail_for_a_missing_shadow_worktree_with_a_try_hint(
    git_repo: Path, capsys
):
    # No `init` was run: there is no shadow worktree yet.
    exit_code = main(["doctor", str(git_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "FAIL" in combined
    assert "worktree" in combined.lower()
    assert "Try:" in combined


def test_doctor_fix_creates_the_missing_shadow_worktree_without_touching_source(
    git_repo: Path, capsys
):
    original_app_py = (git_repo / "app.py").read_text()

    exit_code = main(["doctor", str(git_repo), "--fix"])
    capsys.readouterr()

    config = load_config(git_repo)
    worktree = ShadowWorktree(config)

    assert exit_code == 0
    assert worktree.exists()
    assert (git_repo / "app.py").read_text() == original_app_py


def test_doctor_reports_a_failing_claude_cli_provider_with_its_real_message(
    git_repo: Path, capsys, monkeypatch
):
    main(["init", str(git_repo)])
    capsys.readouterr()
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\n"
        'shadow_dir = ".shadowci/shadow"\n'
        'provider = "claude-cli"\n'
    )

    failure_text = "Failed to authenticate: OAuth session expired and could not be refreshed"
    original_run = subprocess.run

    def _fake_run(args, **kwargs):
        command = args if isinstance(args, str) else " ".join(str(part) for part in args)
        if "claude" in command:
            return subprocess.CompletedProcess(args, 1, stdout=failure_text, stderr="")
        return original_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    exit_code = main(["doctor", str(git_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert exit_code == 1
    assert "FAIL" in combined
    assert failure_text in combined
    assert "Try:" in combined


def test_doctor_reports_a_stale_ghost_patch(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()
    _write_ghost_patch(git_repo, "staleaa01", STALE_FIX, "fp-stale")

    exit_code = main(["doctor", str(git_repo)])
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "staleaa01" in combined or "stale" in combined.lower()
    assert exit_code in (0, 1)


def test_doctor_fix_removes_stale_ghost_patches_but_keeps_ready_ones_and_source_files(
    git_repo: Path, capsys
):
    main(["init", str(git_repo)])
    capsys.readouterr()
    ready_patch = _write_ghost_patch(git_repo, "readyaa01", READY_FIX, "fp-ready")
    stale_patch = _write_ghost_patch(git_repo, "staleaa02", STALE_FIX, "fp-stale")
    original_app_py = (git_repo / "app.py").read_text()

    exit_code = main(["doctor", str(git_repo), "--fix"])
    capsys.readouterr()

    assert exit_code == 0
    assert not stale_patch.exists()
    assert not stale_patch.with_suffix(".json").exists()
    assert ready_patch.exists()
    assert ready_patch.with_suffix(".json").exists()
    assert (git_repo / "app.py").read_text() == original_app_py
