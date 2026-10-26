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


def test_watch_builds_and_saves_a_ghost_patch_on_a_verified_fix(git_repo: Path, capsys):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
    )
    _run_git(["add", "tests"], cwd=git_repo)
    _run_git(["commit", "-q", "-m", "add a real regression test"], cwd=git_repo)

    result: dict[str, int] = {}

    def run_watch() -> None:
        import shadowci.cli as cli_module
        from shadowci.providers.scripted import ScriptedProvider

        fix = (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a + b + 1\n"
            "+    return a + b\n"
        )
        test = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
        original_create_provider = cli_module.create_provider
        cli_module.create_provider = lambda name: ScriptedProvider(tests=[test], fixes=[fix])
        try:
            result["code"] = main(
                ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
            )
        finally:
            cli_module.create_provider = original_create_provider

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b + 1\n")
    thread.join(timeout=10)

    out = capsys.readouterr().out

    assert result.get("code") == 0
    assert "ghost patch verified" in out
    assert "apply with: shadowci apply-ghost" in out

    ghost_dir = git_repo / ".shadowci" / "ghost"
    patches = list(ghost_dir.glob("*.patch"))
    assert len(patches) == 1


def test_apply_ghost_applies_a_saved_patch_to_the_live_tree(git_repo: Path, capsys):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text(
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a + b  # fixed\n"
    )

    exit_code = main(["apply-ghost", str(patch_file), str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "applied" in out
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b  # fixed\n"


def test_apply_ghost_reports_a_missing_patch_file(git_repo: Path, capsys):
    exit_code = main(["apply-ghost", str(git_repo / "nope.patch"), str(git_repo)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no such patch file" in captured.err


def test_apply_ghost_reports_a_patch_that_no_longer_applies(git_repo: Path, capsys):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text(
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b  # fixed\n"
    )

    exit_code = main(["apply-ghost", str(patch_file), str(git_repo)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "error:" in captured.err
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


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
