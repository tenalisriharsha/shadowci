import threading
import time
from pathlib import Path

from shadowci.cli import main


def test_init_creates_config_and_shadow_worktree(git_repo: Path, capsys):
    exit_code = main(["init", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert (git_repo / "shadowci.toml").exists()
    assert (git_repo / ".shadowci" / "shadow").exists()
    assert "shadow worktree ready" in out


def test_init_is_idempotent(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    exit_code = main(["init", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "already exists" in out


def test_status_reports_configuration(git_repo: Path, capsys):
    main(["init", str(git_repo)])
    capsys.readouterr()

    exit_code = main(["status", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "project root" in out
    assert "shadow worktree" in out
    assert "scripted" in out


def test_watch_single_cycle_runs_cleanly(git_repo: Path, capsys):
    exit_code = main(["watch", str(git_repo), "--max-cycles", "1", "--interval", "0.01"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "watching" in out


def test_watch_detects_changeset_and_syncs_into_shadow_tree(git_repo: Path, capsys):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "feature.py").write_text("def feature():\n    return True\n")
    thread.join(timeout=10)

    out = capsys.readouterr().out
    shadow_copy = git_repo / ".shadowci" / "shadow" / "feature.py"

    assert result.get("code") == 0
    assert "created" in out
    assert "changeset ready" in out
    assert shadow_copy.exists()
    assert shadow_copy.read_text() == (git_repo / "feature.py").read_text()
