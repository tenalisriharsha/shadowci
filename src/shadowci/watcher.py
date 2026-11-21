"""File watching.

`FileWatcher` is an abstract interface so a more efficient OS-event-backed
implementation (e.g. built on `watchdog`) can be dropped in later without
touching any downstream code. `PollingFileWatcher` is the dependency-free
implementation used by default and in tests: it snapshots a stat key
(mtime/ctime/inode/size) of every in-scope file on each `poll()` call and
diffs against the previous snapshot.

The stat key alone can't tell a real content change from metadata-only
noise (`chmod`, `touch`) or confirm a change whose mtime was put back
(`cp -p`, `rsync -a`). So a content hash is kept per file and only
recomputed when the stat key changes; a `MODIFIED` event fires only when
that hash actually differs. This keeps idle polling free of file reads
while still catching same-size, same-mtime edits.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod
from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent

# (mtime_ns, ctime_ns, inode, size) per path
_StatKey = tuple[int, int, int, int]
_Snapshot = dict[Path, _StatKey]


class FileWatcher(ABC):
    """Abstract interface for anything that can report file changes."""

    @abstractmethod
    def poll(self) -> list[FileChangeEvent]:
        """Return changes observed since the previous call to `poll()`."""
        raise NotImplementedError


class PollingFileWatcher(FileWatcher):
    """Snapshot-diff watcher: no OS-level file-event dependency required."""

    def __init__(self, config: ShadowCIConfig) -> None:
        self._config = config
        self._root = config.project_root
        self._hashes: dict[Path, bytes] = {}
        self._previous: _Snapshot = self._snapshot()
        for path in self._previous:
            self._hashes[path] = self._hash(path)

    def _iter_watched_files(self) -> list[Path]:
        matches: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dir_path = Path(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in self._config.ignore_dirs
            ]
            for filename in filenames:
                path = dir_path / filename
                relative = path.relative_to(self._root)
                if self._config.should_watch(relative):
                    matches.append(path)
        return matches

    def _snapshot(self) -> _Snapshot:
        snapshot: _Snapshot = {}
        for path in self._iter_watched_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[path] = (
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_ino,
                stat.st_size,
            )
        return snapshot

    @staticmethod
    def _hash(path: Path) -> bytes:
        try:
            return hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            return b""

    def poll(self) -> list[FileChangeEvent]:
        current = self._snapshot()
        events: list[FileChangeEvent] = []
        hashes = dict(self._hashes)

        for path, stat in current.items():
            if path not in self._previous:
                events.append(FileChangeEvent(path=path, kind=ChangeKind.CREATED))
                hashes[path] = self._hash(path)
            elif self._previous[path] != stat:
                new_hash = self._hash(path)
                if hashes.get(path) != new_hash:
                    events.append(FileChangeEvent(path=path, kind=ChangeKind.MODIFIED))
                hashes[path] = new_hash

        for path in self._previous:
            if path not in current:
                events.append(FileChangeEvent(path=path, kind=ChangeKind.DELETED))
                hashes.pop(path, None)

        self._previous = current
        self._hashes = hashes
        return events
