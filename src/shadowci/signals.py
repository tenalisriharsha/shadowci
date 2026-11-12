"""Durable record of accept/reject/ignore signals, for the InterruptionPolicy.

Through Phase 3, every verified ghost patch is treated identically: always
saved, always printed, with no memory of what the user actually did with
fixes like it before. `SignalStore` is where that history lives — a flat
JSON file under `.shadowci/`, keyed by a fingerprint (see
`interruption.fingerprint_for_patch`) so later patches of "the same kind"
can be scored against what happened last time.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from shadowci.events import Signal

DEFAULT_SIGNALS_FILENAME = ".shadowci/signals.json"


@dataclass(frozen=True)
class SignalRecord:
    """One recorded accept/reject/ignore for a given fingerprint.

    `reason` (issue #40) is set only on a reject, to say *why* — e.g.
    `"intentional-change"` when the user rejected a patch because their
    own uncommitted edit was intentional, not because the fix was wrong.
    It defaults to `None`, so a signals file written before this field
    existed still loads.
    """

    fingerprint: str
    signal: Signal
    patch_digest: str
    recorded_at: float
    reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "fingerprint": self.fingerprint,
            "signal": self.signal.value,
            "patch_digest": self.patch_digest,
            "recorded_at": self.recorded_at,
            "reason": self.reason,
        }

    @staticmethod
    def from_dict(data: dict) -> "SignalRecord":
        return SignalRecord(
            fingerprint=data["fingerprint"],
            signal=Signal(data["signal"]),
            patch_digest=data["patch_digest"],
            recorded_at=float(data["recorded_at"]),
            reason=data.get("reason"),
        )


class SignalStore:
    """Appends/loads `SignalRecord`s to/from a JSON file under `.shadowci/`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def all(self) -> tuple[SignalRecord, ...]:
        if not self._path.exists():
            return ()
        raw = self._path.read_text().strip()
        if not raw:
            return ()
        return tuple(SignalRecord.from_dict(item) for item in json.loads(raw))

    def for_fingerprint(self, fingerprint: str) -> tuple[SignalRecord, ...]:
        return tuple(record for record in self.all() if record.fingerprint == fingerprint)

    def record(
        self,
        fingerprint: str,
        signal: Signal,
        patch_digest: str,
        reason: str | None = None,
    ) -> SignalRecord:
        """Append a new signal and persist the full history."""
        new_record = SignalRecord(
            fingerprint=fingerprint,
            signal=signal,
            patch_digest=patch_digest,
            recorded_at=time.time(),
            reason=reason,
        )
        records = (*self.all(), new_record)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps([record.to_dict() for record in records], indent=2))
        return new_record
