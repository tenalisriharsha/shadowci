from pathlib import Path

from shadowci.events import ChangeSet, EvidenceCard, GhostPatch, Signal
from shadowci.interruption import (
    InterruptionDecision,
    InterruptionPolicy,
    fingerprint_for_patch,
)
from shadowci.signals import SignalStore


def _patch(originally_failed_checks: tuple[str, ...] = ("pytest-targeted",)) -> GhostPatch:
    evidence = EvidenceCard(
        summary="fix",
        failing_test_before="def test_x(): assert False",
        originally_failed_checks=originally_failed_checks,
    )
    return GhostPatch(diff="--- a\n+++ b\n", evidence=evidence, changeset=ChangeSet(events=()))


def test_fingerprint_is_sorted_and_stable():
    patch_a = _patch(("mypy", "pytest-targeted"))
    patch_b = _patch(("pytest-targeted", "mypy"))
    assert fingerprint_for_patch(patch_a) == fingerprint_for_patch(patch_b)


def test_fingerprint_falls_back_when_no_checks_recorded():
    assert fingerprint_for_patch(_patch(())) == "(no-checks)"


def test_no_history_stays_silent(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    policy = InterruptionPolicy(store)

    decision = policy.evaluate(_patch())

    assert decision == InterruptionDecision(
        interrupt=False, score=0.0, fingerprint="pytest-targeted", sample_size=0
    )


def test_too_little_history_stays_silent(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    policy = InterruptionPolicy(store, min_signals=2)

    decision = policy.evaluate(_patch())

    assert decision.interrupt is False
    assert decision.sample_size == 1


def test_enough_accepts_surfaces_the_patch(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.ACCEPT, "b")
    policy = InterruptionPolicy(store, threshold=0.5, min_signals=2)

    decision = policy.evaluate(_patch())

    assert decision.interrupt is True
    assert decision.score == 1.0
    assert decision.sample_size == 2


def test_rejects_outweigh_accepts_and_stay_silent(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.REJECT, "b")
    store.record("pytest-targeted", Signal.REJECT, "c")
    policy = InterruptionPolicy(store, threshold=0.34, min_signals=2)

    decision = policy.evaluate(_patch())

    assert decision.interrupt is False
    assert round(decision.score, 2) == -0.33


def test_ignores_count_toward_sample_size_but_not_score(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.IGNORE, "b")
    policy = InterruptionPolicy(store, threshold=0.5, min_signals=2)

    decision = policy.evaluate(_patch())

    assert decision.sample_size == 2
    assert decision.score == 0.5
    assert decision.interrupt is True


def test_different_fingerprints_do_not_share_history(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("mypy", Signal.ACCEPT, "a")
    store.record("mypy", Signal.ACCEPT, "b")
    policy = InterruptionPolicy(store, threshold=0.5, min_signals=2)

    decision = policy.evaluate(_patch(("pytest-targeted",)))

    assert decision.sample_size == 0
    assert decision.interrupt is False


def test_evaluate_fingerprint_matches_evaluate(tmp_path: Path):
    store = SignalStore(tmp_path / "signals.json")
    store.record("pytest-targeted", Signal.ACCEPT, "a")
    store.record("pytest-targeted", Signal.ACCEPT, "b")
    policy = InterruptionPolicy(store, threshold=0.5, min_signals=2)

    assert policy.evaluate(_patch()) == policy.evaluate_fingerprint("pytest-targeted")
