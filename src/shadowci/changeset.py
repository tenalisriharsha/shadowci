"""Decide when a burst of file-save events is actually "done".

ShadowCI must never chase half-typed code, so a buffer of `FileChangeEvent`s
is only promoted to a stable `ChangeSet` once three conditions all hold:

1. **Debounce** — at least `debounce_seconds` has passed since the most
   recent event, so we're not reacting to the very next keystroke.
2. **Idle** — at least `idle_seconds` (strictly longer than the debounce
   window) has passed since the most recent event, so a brief typing pause
   isn't mistaken for "finished editing".
3. **Syntax/compile check** — every still-existing Python file touched by
   the buffer parses cleanly. A file that fails to parse means the edit is
   still mid-flight; the buffer keeps accumulating instead of promoting.

The syntax check is applied per file, not to the buffer as a whole: a file
that stays broken is held back on its own, but it never blocks promotion of
other, unrelated files that already parse cleanly.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent


class ChangeSetDetector:
    """Buffers file-change events and promotes them once they're stable."""

    def __init__(self, config: ShadowCIConfig) -> None:
        self._config = config
        self._buffer: list[FileChangeEvent] = []
        self._last_event_time: float | None = None

    @property
    def pending_count(self) -> int:
        return len(self._buffer)

    def add_events(self, events: list[FileChangeEvent]) -> None:
        if not events:
            return
        self._buffer.extend(events)
        self._last_event_time = max(event.timestamp for event in events)

    def poll(self, now: float | None = None) -> ChangeSet | None:
        """Return a stable `ChangeSet` if the buffer is ready, else None."""
        if not self._buffer or self._last_event_time is None:
            return None

        now = time.time() if now is None else now
        elapsed = now - self._last_event_time

        if elapsed < self._config.debounce_seconds:
            return None
        if elapsed < self._config.idle_seconds:
            return None

        ready, blocked = self._partition_by_syntax_validity()
        if not ready:
            return None

        changeset = ChangeSet(events=tuple(ready), detected_at=now)
        self._buffer = blocked
        if not blocked:
            self._last_event_time = None
        return changeset

    def _partition_by_syntax_validity(
        self,
    ) -> tuple[list[FileChangeEvent], list[FileChangeEvent]]:
        """Split the buffer into events ready to promote and events still
        blocked by their own file's syntax error."""
        broken_paths: set[Path] = set()
        checked: set[Path] = set()
        for event in self._buffer:
            if event.path in checked:
                continue
            checked.add(event.path)
            if event.kind is ChangeKind.DELETED:
                continue
            if event.path.suffix != ".py":
                continue
            if not self._is_valid_python(event.path):
                broken_paths.add(event.path)

        if broken_paths:
            print(
                "shadowci: holding back changeset, still broken: "
                + ", ".join(str(path) for path in sorted(broken_paths))
            )

        ready = [event for event in self._buffer if event.path not in broken_paths]
        blocked = [event for event in self._buffer if event.path in broken_paths]
        return ready, blocked

    @staticmethod
    def _is_valid_python(path: Path) -> bool:
        try:
            source = path.read_text()
        except OSError:
            return True  # file vanished mid-check; let the delete event handle it
        try:
            ast.parse(source)
        except SyntaxError:
            return False
        return True
