"""Recorded coding-session format: a replayable sequence of timestamped edits.

A `RecordedSession` is the evaluation harness's input: the starting files a
project had on disk, and a flat list of edits (write-this-content or
delete-this-path), each tagged with an offset in seconds from the start of
the session instead of a wall-clock timestamp. The harness drives those
offsets through a simulated clock, so a whole session replays in
milliseconds regardless of how much idle time it records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from shadowci.events import ChangeKind


@dataclass(frozen=True)
class RecordedEdit:
    """One timestamped edit in a session, relative to the session's start."""

    relative_path: str
    kind: ChangeKind
    offset_seconds: float
    content: str | None = None  # unused (and expected None) for DELETED


@dataclass(frozen=True)
class RecordedSession:
    """A replayable session: the files it starts from plus its edit sequence."""

    name: str
    initial_files: dict[str, str] = field(default_factory=dict)
    edits: tuple[RecordedEdit, ...] = ()
    description: str = ""


def session_to_dict(session: RecordedSession) -> dict:
    return {
        "name": session.name,
        "description": session.description,
        "initial_files": session.initial_files,
        "edits": [
            {
                "relative_path": edit.relative_path,
                "kind": edit.kind.value,
                "offset_seconds": edit.offset_seconds,
                "content": edit.content,
            }
            for edit in session.edits
        ],
    }


def session_from_dict(data: dict) -> RecordedSession:
    return RecordedSession(
        name=data["name"],
        description=data.get("description", ""),
        initial_files=dict(data.get("initial_files", {})),
        edits=tuple(
            RecordedEdit(
                relative_path=edit["relative_path"],
                kind=ChangeKind(edit["kind"]),
                offset_seconds=float(edit["offset_seconds"]),
                content=edit.get("content"),
            )
            for edit in data.get("edits", [])
        ),
    )


def save_session(session: RecordedSession, path: Path) -> None:
    path.write_text(json.dumps(session_to_dict(session), indent=2) + "\n")


def load_session(path: Path) -> RecordedSession:
    return session_from_dict(json.loads(path.read_text()))
