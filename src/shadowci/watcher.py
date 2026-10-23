"""File watching.

`FileWatcher` is an abstract interface so a more efficient OS-event-backed
implementation (e.g. built on `watchdog`) can be dropped in later without
touching any downstream code. `PollingFileWatcher` is the dependency-free
implementation used by default and in tests: it snapshots mtimes/sizes of
every in-scope file on each `poll()` call and diffs against the previous
snapshot.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent

# (mtime, size) per path
_Snapshot = dict[Path, tuple[float, int]]


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
        self._previous: _Snapshot = self._snapshot()

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
            snapshot[path] = (stat.st_mtime, stat.st_size)
        return snapshot

    def poll(self) -> list[FileChangeEvent]:
        current = self._snapshot()
        events: list[FileChangeEvent] = []

        for path, stat in current.items():
            if path not in self._previous:
                events.append(FileChangeEvent(path=path, kind=ChangeKind.CREATED))
            elif self._previous[path] != stat:
                events.append(FileChangeEvent(path=path, kind=ChangeKind.MODIFIED))

        for path in self._previous:
            if path not in current:
                events.append(FileChangeEvent(path=path, kind=ChangeKind.DELETED))

        self._previous = current
        return events
