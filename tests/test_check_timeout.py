"""Regression tests for issue #72: check runners have no timeout.

Today `checks/base.py::run_subprocess` calls `subprocess.run` with no
`timeout=`. A single test that never finishes (infinite loop, deadlock, a
hung network wait) blocks the check forever, which blocks `watch`/`radar`
forever: nothing is printed and no later edit is ever checked.

These tests drive the real pipeline (`pipeline.run_checks_and_maybe_escalate`
and the `watch` CLI loop) against a project whose test suite genuinely
hangs, using real subprocesses — not mocks — since the whole point of the
bug is real process lifetime. Every hang used below is bounded (a plain
`time.sleep`, not an infinite loop) so that an unfixed run still terminates
instead of wedging the test suite itself; the bound is chosen well above
the configured timeout so the two are easy to tell apart on duration alone.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree

CONFIGURED_TIMEOUT_SECONDS = 1
# Comfortably longer than the configured timeout, but still bounded so an
# unfixed run finishes (slowly) instead of hanging the test suite forever.
HANG_SECONDS = 6


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], repo)
    _run_git(["commit", "-q", "-m", message], repo)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _hanging_test_source(pid_file: Path) -> str:
    """A pytest test that spawns a grandchild process (whose pid it records
    to `pid_file`) and then blocks for `HANG_SECONDS` itself — standing in
    for a deadlock or an infinite loop in a covered test."""
    return (
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "\n"
        "\n"
        "def test_hangs():\n"
        "    grandchild = subprocess.Popen(\n"
        "        [sys.executable, '-c', 'import time; time.sleep(120)']\n"
        "    )\n"
        f"    with open({str(pid_file)!r}, 'w') as handle:\n"
        "        handle.write(str(grandchild.pid))\n"
        f"    time.sleep({HANG_SECONDS})\n"
    )


def test_hanging_check_times_out_without_escalating_and_kills_the_process_group(
    git_repo: Path, tmp_path: Path
):
    pid_file = tmp_path / "grandchild.pid"
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(_hanging_test_source(pid_file))
    _commit_all(git_repo, "add a hanging test")

    config = _config(git_repo)
    config.check_timeout_seconds = CONFIGURED_TIMEOUT_SECONDS

    live_file = git_repo / "app.py"
    live_file.write_text("def add(a, b):\n    return a + b  # trivial edit\n")
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(config)
    worktree.sync(list(changeset.events))

    calls: list[str] = []

    class RecordingProvider(ScriptedProvider):
        def write_regression_test(self, changeset, check_results):
            calls.append("write_regression_test")
            return super().write_regression_test(changeset, check_results)

        def propose_fix(self, changeset, check_results):
            calls.append("propose_fix")
            return super().propose_fix(changeset, check_results)

    start = time.monotonic()
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: RecordingProvider()
    )
    duration = time.monotonic() - start

    assert duration < CONFIGURED_TIMEOUT_SECONDS + 4, (
        f"a hanging check must be killed near check_timeout_seconds="
        f"{CONFIGURED_TIMEOUT_SECONDS}, but the pipeline took {duration:.1f}s "
        f"(the test itself hangs for {HANG_SECONDS}s with no timeout applied)"
    )

    timed_out_results = [r for r in outcome.check_results if getattr(r, "timed_out", False)]
    assert timed_out_results, (
        "expected at least one CheckResult marked timed_out after a check hung past "
        f"check_timeout_seconds, got {outcome.check_results}"
    )

    assert calls == [], (
        f"a timed-out check must never escalate to the provider, got calls={calls}"
    )

    # Give process teardown a brief moment to land, then confirm the whole
    # process group (not just the direct pytest child) was killed.
    deadline = time.monotonic() + 3
    pid = None
    while time.monotonic() < deadline:
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                break
            except ValueError:
                pass
        time.sleep(0.1)

    assert pid is not None, "the hanging test never got to record its grandchild's pid"
    time.sleep(0.5)
    assert not _pid_alive(pid), (
        "a check timeout must kill the whole process group, leaving no grandchild "
        "process of the check still alive"
    )


def test_ghost_confirmation_timeout_does_not_save_a_patch(git_repo: Path):
    """Issue #72: the post-fix confirmation run inside `GhostPatchBuilder`
    re-runs the full suite, not just the targeted test(s). If that full-suite
    run hangs, the patch must not be saved — and verifying it must not take
    anywhere near as long as the hang itself.
    """
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    pid_file = git_repo / "grandchild.pid"
    (tests_dir / "test_unrelated_hangs.py").write_text(_hanging_test_source(pid_file))
    _commit_all(git_repo, "add tests, including one unrelated hanging test")

    config = _config(git_repo)
    config.check_timeout_seconds = CONFIGURED_TIMEOUT_SECONDS

    live_file = git_repo / "app.py"
    live_file.write_text("def add(a, b):\n    return a + b + 1\n")  # buggy edit
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(config)
    worktree.sync(list(changeset.events))

    fix = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a + b + 1\n"
        "+    return a + b\n"
    )
    regression_test = "from app import add\n\n\ndef test_add_regression():\n    assert add(2, 3) == 5\n"

    start = time.monotonic()
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[fix]),
    )
    duration = time.monotonic() - start

    assert duration < CONFIGURED_TIMEOUT_SECONDS + 5, (
        "confirming (or failing to confirm) a ghost patch must not block for "
        f"anywhere near the full {HANG_SECONDS}s hang; took {duration:.1f}s"
    )

    assert outcome.ghost_patch is None, (
        "a ghost patch must not be saved when its confirmation run timed out"
    )
    assert outcome.ghost_patch_error is not None
    lowered = outcome.ghost_patch_error.lower()
    assert "confirm" in lowered and "timed out" in lowered, (
        f"expected a confirmation-timed-out error, got: {outcome.ghost_patch_error!r}"
    )

    assert pipeline.list_ghost_patches(config.project_root) == ()


def test_watch_times_out_a_hanging_check_and_still_checks_a_later_edit(git_repo: Path, capsys):
    tests_dir = git_repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_app():\n    assert True\n")
    pid_file = git_repo / "grandchild.pid"
    flaky_module = git_repo / "flaky.py"
    flaky_module.write_text("VALUE = 1\n")
    (tests_dir / "test_flaky.py").write_text(_hanging_test_source(pid_file))
    _commit_all(git_repo, "add app, a quick test, and a hanging test")

    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\n"
        "debounce_seconds = 0.03\n"
        "idle_seconds = 0.08\n"
        f"check_timeout_seconds = {CONFIGURED_TIMEOUT_SECONDS}\n"
    )

    from shadowci.cli import main

    result: dict[str, int] = {}

    def run_watch() -> None:
        result["code"] = main(
            ["watch", str(git_repo), "--max-cycles", "400", "--interval", "0.02"]
        )

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.3)

    # First edit: touches the module whose targeted test hangs.
    flaky_module.write_text("VALUE = 2\n")
    time.sleep(CONFIGURED_TIMEOUT_SECONDS + 2.5)

    # Second edit, made only after the first should already have timed out:
    # its own (fast, passing) targeted test must still get checked normally.
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # second edit\n")
    time.sleep(1.0)

    thread.join(timeout=15)
    if thread.is_alive():
        # Safety net: don't leave a background watcher running past this test.
        thread.join(timeout=10)

    out = capsys.readouterr().out

    assert "TIMEOUT" in out, (
        "expected the hanging check to be reported as a TIMEOUT, got:\n" + out
    )
    assert "check_timeout_seconds" in out, (
        "expected a hint pointing at check_timeout_seconds after a timeout, got:\n" + out
    )
    assert out.count("changeset ready") >= 2, (
        "watch must keep processing change sets after a timeout, got:\n" + out
    )
    assert "pytest-targeted] PASS" in out, (
        "the second, unrelated edit must still be checked normally after the "
        "first check timed out, got:\n" + out
    )
