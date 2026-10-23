from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind
from shadowci.watcher import PollingFileWatcher


def _config(root: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=root)


def test_poll_detects_created_file(tmp_path: Path):
    watcher = PollingFileWatcher(_config(tmp_path))
    assert watcher.poll() == []

    (tmp_path / "new.py").write_text("x = 1\n")
    events = watcher.poll()

    assert len(events) == 1
    assert events[0].kind is ChangeKind.CREATED
    assert events[0].path == tmp_path / "new.py"


def test_poll_detects_modified_file(tmp_path: Path):
    target = tmp_path / "existing.py"
    target.write_text("x = 1\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()  # baseline snapshot now includes existing.py

    target.write_text("x = 2  # changed and longer\n")
    events = watcher.poll()

    assert len(events) == 1
    assert events[0].kind is ChangeKind.MODIFIED


def test_poll_detects_deleted_file(tmp_path: Path):
    target = tmp_path / "doomed.py"
    target.write_text("x = 1\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()

    target.unlink()
    events = watcher.poll()

    assert len(events) == 1
    assert events[0].kind is ChangeKind.DELETED
    assert events[0].path == target


def test_poll_ignores_files_outside_watch_extensions(tmp_path: Path):
    watcher = PollingFileWatcher(_config(tmp_path))
    (tmp_path / "notes.txt").write_text("hello\n")
    assert watcher.poll() == []


def test_poll_ignores_configured_directories(tmp_path: Path):
    config = ShadowCIConfig(project_root=tmp_path, ignore_dirs=(".git", "vendor"))
    watcher = PollingFileWatcher(config)

    vendor_dir = tmp_path / "vendor"
    vendor_dir.mkdir()
    (vendor_dir / "lib.py").write_text("x = 1\n")

    assert watcher.poll() == []
