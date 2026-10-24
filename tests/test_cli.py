import subprocess
import threading
import time
from pathlib import Path

from shadowci.cli import main


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


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
    assert "check [pytest]" in out


def test_watch_skip_checks_does_not_run_them(git_repo: Path, capsys):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            [
                "watch",
                str(git_repo),
                "--max-cycles",
                "30",
                "--interval",
                "0.02",
                "--skip-checks",
            ]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "feature.py").write_text("def feature():\n    return True\n")
    thread.join(timeout=10)

    out = capsys.readouterr().out

    assert result.get("code") == 0
    assert "changeset ready" in out
    assert "check [pytest]" not in out


def test_watch_escalates_to_scripted_provider_on_a_failing_check(git_repo: Path, capsys):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text(
        "def test_app():\n    assert False\n"
    )
    _run_git(["add", "tests"], cwd=git_repo)
    _run_git(["commit", "-q", "-m", "add a failing test"], cwd=git_repo)

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    thread.join(timeout=10)

    out = capsys.readouterr().out

    assert result.get("code") == 0
    assert "check [pytest-targeted]" in out
    assert "FAIL" in out
    assert "escalating to provider 'scripted'" in out
    assert "regression test:" in out
    assert "proposed fix:" in out


def test_watch_unknown_provider_skips_escalation(git_repo: Path, capsys):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
        'provider = "not-a-real-provider"\n'
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text("def test_app():\n    assert False\n")
    _run_git(["add", "tests"], cwd=git_repo)
    _run_git(["commit", "-q", "-m", "add a failing test"], cwd=git_repo)

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    thread.join(timeout=10)

    captured = capsys.readouterr()

    assert result.get("code") == 0
    assert "skipping escalation: unknown provider" in captured.err


def test_watch_reports_provider_errors_without_crashing(git_repo: Path, capsys, monkeypatch):
    import shadowci.cli as cli_module
    from shadowci.providers.scripted import ScriptedProvider

    class _ExplodingProvider(ScriptedProvider):
        name = "scripted"

        def write_regression_test(self, changeset, check_results):
            raise RuntimeError("provider blew up")

    monkeypatch.setattr(cli_module, "create_provider", lambda name: _ExplodingProvider())

    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text("def test_app():\n    assert False\n")
    _run_git(["add", "tests"], cwd=git_repo)
    _run_git(["commit", "-q", "-m", "add a failing test"], cwd=git_repo)

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    thread.join(timeout=10)

    captured = capsys.readouterr()

    assert result.get("code") == 0
    assert "provider error: provider blew up" in captured.err
