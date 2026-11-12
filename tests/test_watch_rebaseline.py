"""Regression tests for issue #53, driven through the real `shadowci watch`
CLI loop instead of calling `ShadowWorktree` directly.

`watch`, `radar` and `evaluate` must all route through one shared place
that keeps the shadow worktree rebaselined (same `HEAD`, same files as the
live tree) whenever a change set is ready and the live `HEAD` has moved.
These tests pin the user-visible contract for `watch`:

- a commit made between two edits is picked up (shadow `HEAD` tracks it);
- a file changed by a commit made while the watcher wasn't running is
  mirrored once a new watcher starts and the next change set lands;
- a branch switch (many files changing at once, but because of git, not a
  user edit) never triggers checks or constructs a provider;
- while a merge is in progress (`MERGE_HEAD` present), nothing is synced
  or checked.

They use the same background-thread-plus-timed-edit pattern already used
by `tests/test_revert_check.py::test_watch_asks_instead_of_saving_a_fix_for_an_intentional_change`,
with a generous debounce/idle window so the assertions aren't timing-
sensitive on a slow machine.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

from shadowci.cli import main
from shadowci.config import ShadowCIConfig
from shadowci.shadow_worktree import ShadowWorktree

DEBOUNCE_IDLE_TOML = "[shadowci]\ndebounce_seconds = 0.03\nidle_seconds = 0.08\n"
SETTLE_SECONDS = 0.4


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _git_ok(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _head_sha(cwd: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


def _diff_head(cwd: Path) -> str:
    return _run_git(["diff", "HEAD"], cwd=cwd).stdout


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _run_watch_in_background(
    repo: Path, max_cycles: int = 300, interval: float = 0.02
) -> tuple[threading.Thread, dict]:
    result: dict = {}

    def run() -> None:
        result["code"] = main(
            ["watch", str(repo), "--max-cycles", str(max_cycles), "--interval", str(interval)]
        )

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_watch_keeps_shadow_head_in_sync_with_live_head_across_a_commit(
    git_repo: Path,
):
    (git_repo / ".gitignore").write_text(".shadowci/\n")
    (git_repo / "shadowci.toml").write_text(DEBOUNCE_IDLE_TOML)

    thread, _result = _run_watch_in_background(git_repo)
    time.sleep(SETTLE_SECONDS)

    live_app = git_repo / "app.py"
    live_app.write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n\n"
        "def median(values):\n"
        "    s = sorted(values)\n"
        "    n = len(s)\n"
        "    mid = n // 2\n"
        "    if n % 2:\n"
        "        return s[mid]\n"
        "    return (s[mid - 1] + s[mid]) / 2\n"
    )
    time.sleep(SETTLE_SECONDS)

    _commit_all(git_repo, "add median")

    live_app.write_text(live_app.read_text() + "\n# a trailing, uncommitted comment\n")
    time.sleep(SETTLE_SECONDS)

    thread.join(timeout=15)

    config = _config(git_repo)
    worktree = ShadowWorktree(config)

    assert _head_sha(worktree.path) == _head_sha(git_repo)
    assert _diff_head(worktree.path) == _diff_head(git_repo)


def test_watch_mirrors_a_file_changed_by_a_commit_made_while_watch_was_off(
    git_repo: Path,
):
    _write(git_repo, "text.py", "def shout(s):\n    return s.upper()\n")
    _commit_all(git_repo, "add text.py")

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()  # shadow created while this older commit is live

    # No watcher is running: commit a change to text.py.
    _write(
        git_repo,
        "text.py",
        "def shout(s):\n    return s.upper()\n\n\ndef whisper(s):\n    return s.lower()\n",
    )
    _commit_all(git_repo, "add whisper")

    (git_repo / "shadowci.toml").write_text(DEBOUNCE_IDLE_TOML)

    thread, _result = _run_watch_in_background(git_repo)
    time.sleep(SETTLE_SECONDS)

    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # tweak\n")
    time.sleep(SETTLE_SECONDS)

    thread.join(timeout=15)

    assert (worktree.path / "text.py").read_text() == (git_repo / "text.py").read_text()


def test_watch_branch_switch_triggers_no_checks_and_no_provider_call(
    git_repo: Path, monkeypatch
):
    _write(git_repo, "tests/test_app.py", "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    _commit_all(git_repo, "add a regression test")

    original_branch = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo
    ).stdout.strip()

    _run_git(["checkout", "-b", "feature", "-q"], cwd=git_repo)
    (git_repo / "app.py").write_text("def add(a, b):\n    return a - b  # breaks test_add\n")
    _commit_all(git_repo, "feature: change add() behavior")
    _run_git(["checkout", original_branch, "-q"], cwd=git_repo)

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    import shadowci.cli as cli_module

    provider_calls: list[str] = []
    original_create_provider = cli_module.create_provider

    def recording_create_provider(name: str):
        provider_calls.append(name)
        return original_create_provider(name)

    monkeypatch.setattr(cli_module, "create_provider", recording_create_provider)

    (git_repo / "shadowci.toml").write_text(DEBOUNCE_IDLE_TOML)

    thread, _result = _run_watch_in_background(git_repo)
    time.sleep(SETTLE_SECONDS)

    _run_git(["checkout", "feature", "-q"], cwd=git_repo)
    time.sleep(SETTLE_SECONDS)

    thread.join(timeout=15)

    assert provider_calls == [], (
        f"a branch switch must never construct a provider, got calls={provider_calls}"
    )
    assert pipeline_has_no_saved_patches(git_repo)
    assert _head_sha(worktree.path) == _head_sha(git_repo)
    assert (worktree.path / "app.py").read_text() == (git_repo / "app.py").read_text()


def pipeline_has_no_saved_patches(root: Path) -> bool:
    from shadowci import pipeline

    return pipeline.list_ghost_patches(root) == ()


def test_watch_skips_sync_during_an_in_progress_merge(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    merge_head_sha = _head_sha(git_repo)
    (git_repo / ".git" / "MERGE_HEAD").write_text(merge_head_sha + "\n")

    (git_repo / "shadowci.toml").write_text(DEBOUNCE_IDLE_TOML)

    thread, _result = _run_watch_in_background(git_repo, max_cycles=60)
    time.sleep(SETTLE_SECONDS)

    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # mid-merge edit\n")
    time.sleep(SETTLE_SECONDS)

    thread.join(timeout=15)

    assert (worktree.path / "app.py").read_text() != (git_repo / "app.py").read_text(), (
        "a change made while MERGE_HEAD exists must not be synced into the shadow"
    )
