"""Telling ShadowCI "don't judge this yet" (issue #40).

Two independent ways to hold back escalation, sharing one message:

- `shadowci pause` / `shadowci resume` hold back escalation project-wide,
  optionally for a fixed duration (`--for 30m`). State lives under
  `.shadowci/pause.json` so it survives between `watch` invocations and,
  given a duration, expires on its own without a `resume` call.
- A `# shadowci: wip` marker line in a changed file holds back escalation
  triggered by edits to that file, with no state to expire — it's live as
  long as the line is there.

Either way, deterministic checks (tests/type checker/linter) still run and
still mirror into the shadow worktree; only the model call is skipped.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

PAUSE_FILENAME = ".shadowci/pause.json"
WIP_MARKER = "# shadowci: wip"
PAUSE_MESSAGE = "paused: no fix will be generated (resume with: shadowci resume)"

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smh])$")
_DURATION_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(text: str) -> float:
    """Parse a duration like `30m`, `2h`, `45s` into seconds."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"invalid duration {text!r} (expected e.g. '30m', '2h', '45s')")
    amount, unit = match.groups()
    return float(amount) * _DURATION_UNIT_SECONDS[unit]


@dataclass(frozen=True)
class PauseState:
    """A recorded pause: when it started, and when (if ever) it ends."""

    paused_at: float
    until: float | None = None

    def active(self, now: float | None = None) -> bool:
        if self.until is None:
            return True
        return (time.time() if now is None else now) < self.until

    def to_dict(self) -> dict:
        return {"paused_at": self.paused_at, "until": self.until}

    @staticmethod
    def from_dict(data: dict) -> "PauseState":
        until = data.get("until")
        return PauseState(
            paused_at=float(data["paused_at"]),
            until=float(until) if until is not None else None,
        )


def pause_path(root: Path) -> Path:
    return root / PAUSE_FILENAME


def pause(root: Path, duration_seconds: float | None = None) -> PauseState:
    """Start a pause, persisted under `.shadowci/`."""
    now = time.time()
    state = PauseState(
        paused_at=now, until=(now + duration_seconds if duration_seconds is not None else None)
    )
    path = pause_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), indent=2))
    return state


def resume(root: Path) -> bool:
    """End a pause. Returns whether there was one to end."""
    path = pause_path(root)
    if not path.exists():
        return False
    path.unlink()
    return True


def current_pause(root: Path) -> PauseState | None:
    """The active pause for `root`, or `None` if there isn't one.

    A pause whose `--for` duration has elapsed is treated as not active
    and its state file is cleared, so it "ends after the duration" with
    no `resume` call needed.
    """
    path = pause_path(root)
    if not path.exists():
        return None
    try:
        state = PauseState.from_dict(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return None
    if state.active():
        return state
    path.unlink(missing_ok=True)
    return None


def file_has_wip_marker(path: Path) -> bool:
    """Whether `path` contains the `# shadowci: wip` marker line."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        return False
    return any(WIP_MARKER in line for line in text.splitlines())
