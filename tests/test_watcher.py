import builtins
import os
import time
from pathlib import Path

import pytest

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


# Issue #74: a same-size edit whose mtime is put back (`cp -p`, `rsync -a`,
# some editors/sync tools) must still be reported, and chmod/touch noise
# with unchanged content must not be.


def test_poll_detects_same_size_edit_with_mtime_put_back(tmp_path: Path):
    target = tmp_path / "calc.py"
    target.write_text("return a + b\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()  # baseline snapshot now includes calc.py

    before = target.stat()
    target.write_text("return a - b\n")  # same size, real content change
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    events = watcher.poll()

    assert len(events) == 1
    assert events[0].kind is ChangeKind.MODIFIED
    assert events[0].path == target


def test_poll_detects_replace_with_same_size_and_mtime(tmp_path: Path):
    target = tmp_path / "calc.py"
    target.write_text("return a + b\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()  # baseline snapshot now includes calc.py

    before = target.stat()
    staging = tmp_path / ".calc.py.tmp"
    staging.write_text("return a - b\n")  # same size, real content change
    os.utime(staging, ns=(before.st_atime_ns, before.st_mtime_ns))
    os.replace(staging, target)
    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    events = watcher.poll()

    assert len(events) == 1
    assert events[0].kind is ChangeKind.MODIFIED
    assert events[0].path == target


def test_poll_ignores_touch_and_chmod_with_unchanged_content(tmp_path: Path):
    target = tmp_path / "calc.py"
    target.write_text("return a + b\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()  # baseline snapshot now includes calc.py

    # touch: bumps mtime (and ctime) with no content change.
    time.sleep(1.1)
    os.utime(target, None)
    assert watcher.poll() == []

    # chmod: bumps ctime with no content change.
    target.chmod(0o644)
    assert watcher.poll() == []


def test_poll_reads_no_file_content_when_no_stat_key_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    watcher = PollingFileWatcher(_config(tmp_path))
    watcher.poll()  # baseline snapshot now includes both files

    calls = []
    real_open = builtins.open
    real_path_open = Path.open
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def counting_open(self_or_file, *args, **kwargs):
        calls.append(self_or_file)
        return real_open(self_or_file, *args, **kwargs)

    def counting_path_open(self, *args, **kwargs):
        calls.append(self)
        return real_path_open(self, *args, **kwargs)

    def counting_read_bytes(self, *args, **kwargs):
        calls.append(self)
        return real_read_bytes(self, *args, **kwargs)

    def counting_read_text(self, *args, **kwargs):
        calls.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    monkeypatch.setattr(Path, "open", counting_path_open)
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    monkeypatch.setattr(Path, "read_text", counting_read_text)

    events = watcher.poll()

    assert events == []
    assert calls == []
