"""Regression tests for issue #40: no way to say a reject was an intentional
change rather than a bad fix.

`SignalRecord`/`SignalStore` had no place to record *why* a patch was
rejected, so every reject — including one triggered only because the
user's own intentional change tripped a check — looked identical to a
genuinely bad fix and dragged down the fingerprint's score the same way.
These tests exercise the new `reason` on a recorded signal, and that an
older signals file written before this field existed still loads.
"""

from __future__ import annotations

import json
from pathlib import Path

from shadowci.events import Signal
from shadowci.signals import SignalStore


def test_record_persists_a_reason_and_round_trips(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")

    record = store.record("pytest-targeted", Signal.REJECT, "abc123", reason="intentional-change")

    assert record.reason == "intentional-change"

    reloaded = SignalStore(store.path)
    assert reloaded.all() == (record,)
    assert '"reason": "intentional-change"' in store.path.read_text()


def test_record_without_a_reason_defaults_to_none(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")

    record = store.record("pytest-targeted", Signal.REJECT, "abc123")

    assert record.reason is None


def test_loading_an_old_signals_file_without_a_reason_key_still_works(tmp_path: Path):
    path = tmp_path / ".shadowci" / "signals.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "pytest-targeted",
                    "signal": "reject",
                    "patch_digest": "abc123",
                    "recorded_at": 1_700_000_000.0,
                }
            ]
        )
    )

    store = SignalStore(path)
    records = store.all()

    assert len(records) == 1
    assert records[0].fingerprint == "pytest-targeted"
    assert records[0].reason is None
