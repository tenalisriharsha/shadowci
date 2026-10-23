"""Core data model shared across ShadowCI's pipeline stages.

These are plain, immutable-where-possible dataclasses with no behavior of
their own — every stage (watcher, change-set detector, shadow worktree,
checks, LLM providers, interruption policy) passes these objects between
each other instead of ad-hoc dicts/tuples.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class ChangeKind(str, Enum):
    """What happened to a watched file."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True)
class FileChangeEvent:
    """A single observed change to a single file."""

    path: Path
    kind: ChangeKind
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ChangeSet:
    """A debounced, idle-confirmed, syntax-clean batch of file changes.

    Only the ChangeSetDetector constructs these — their existence is a
    promise that the edits behind them are not mid-keystroke.
    """

    events: tuple[FileChangeEvent, ...]
    detected_at: float = field(default_factory=time.time)

    @property
    def paths(self) -> tuple[Path, ...]:
        seen: dict[Path, None] = {}
        for event in self.events:
            seen[event.path] = None
        return tuple(seen.keys())


@dataclass(frozen=True)
class CheckResult:
    """The outcome of a single deterministic check (test/type/lint/etc.)."""

    name: str
    passed: bool
    output: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class EvidenceCard:
    """Human-readable proof that a ghost patch is a real, verified fix."""

    summary: str
    failing_test_before: str
    check_results: tuple[CheckResult, ...] = ()

    @property
    def all_checks_passed(self) -> bool:
        return all(result.passed for result in self.check_results)


@dataclass(frozen=True)
class GhostPatch:
    """A verified fix sitting in the shadow worktree, ready to apply."""

    diff: str
    evidence: EvidenceCard
    changeset: ChangeSet


class Signal(str, Enum):
    """User response to a surfaced ghost patch, used by the interruption policy."""

    ACCEPT = "accept"
    REJECT = "reject"
    IGNORE = "ignore"
