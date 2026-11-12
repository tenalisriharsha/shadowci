"""Regression test for issue #40: an intentional-change reject must not
lower the fingerprint's score the way a genuine bad-fix reject does."""

from __future__ import annotations

from pathlib import Path

from shadowci.events import Signal
from shadowci.interruption import InterruptionPolicy
from shadowci.signals import SignalStore


def test_intentional_change_reject_does_not_affect_score_or_sample_size(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.ACCEPT, "b")
    policy = InterruptionPolicy(store, threshold=0.34, min_signals=2)

    before = policy.evaluate_fingerprint("pytest-targeted")

    store.record("pytest-targeted", Signal.REJECT, "c", reason="intentional-change")

    after = policy.evaluate_fingerprint("pytest-targeted")

    assert after.score == before.score
    assert after.sample_size == before.sample_size


def test_ordinary_reject_with_no_reason_still_lowers_the_score(tmp_path: Path):
    store = SignalStore(tmp_path / ".shadowci" / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.ACCEPT, "b")
    policy = InterruptionPolicy(store, threshold=0.34, min_signals=2)

    before = policy.evaluate_fingerprint("pytest-targeted")

    store.record("pytest-targeted", Signal.REJECT, "c")

    after = policy.evaluate_fingerprint("pytest-targeted")

    assert after.score < before.score
    assert after.sample_size == before.sample_size + 1
