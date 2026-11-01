from pathlib import Path

import pytest

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def test_ensure_created_makes_a_real_git_worktree(git_repo: Path):
    worktree = ShadowWorktree(_config(git_repo))
    assert worktree.exists() is False

    path = worktree.ensure_created()

    assert path == git_repo / ".shadowci" / "shadow"
    assert worktree.exists() is True
    assert (path / "app.py").exists()  # mirrors the committed state


def test_ensure_created_is_idempotent(git_repo: Path):
    worktree = ShadowWorktree(_config(git_repo))
    first = worktree.ensure_created()
    second = worktree.ensure_created()
    assert first == second


def test_sync_copies_live_bytes_into_shadow_tree(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    live_file = git_repo / "app.py"
    live_file.write_text("def add(a, b):\n    return a + b + 0\n")

    touched = worktree.sync(
        [FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED)]
    )

    shadow_file = worktree.path / "app.py"
    assert shadow_file.read_text() == live_file.read_text()
    assert touched == [Path("app.py")]


def test_sync_handles_new_file(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    new_file = git_repo / "helper.py"
    new_file.write_text("def helper():\n    return 42\n")

    worktree.sync([FileChangeEvent(path=new_file, kind=ChangeKind.CREATED)])

    assert (worktree.path / "helper.py").read_text() == new_file.read_text()


def test_sync_handles_deleted_file(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()
    assert (worktree.path / "app.py").exists()

    live_file = git_repo / "app.py"
    live_file.unlink()

    worktree.sync([FileChangeEvent(path=live_file, kind=ChangeKind.DELETED)])

    assert not (worktree.path / "app.py").exists()


def test_sync_auto_creates_worktree_if_missing(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    assert worktree.exists() is False

    live_file = git_repo / "app.py"
    worktree.sync([FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED)])

    assert worktree.exists() is True


def test_remove_tears_down_worktree(git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()
    assert worktree.exists() is True

    worktree.remove()

    assert worktree.exists() is False


def test_sync_rejects_write_outside_project_root(tmp_path: Path, git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    outside_file = tmp_path / "outside" / "secret.txt"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("not part of the project\n")

    with pytest.raises(ShadowWorktreeError):
        worktree.sync([FileChangeEvent(path=outside_file, kind=ChangeKind.MODIFIED)])

    assert outside_file.read_text() == "not part of the project\n"


def test_sync_rejects_delete_outside_project_root(tmp_path: Path, git_repo: Path):
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    outside_file = tmp_path / "outside" / "secret.txt"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("not part of the project\n")

    with pytest.raises(ShadowWorktreeError):
        worktree.sync([FileChangeEvent(path=outside_file, kind=ChangeKind.DELETED)])

    assert outside_file.exists()  # the escape attempt must not delete it
