"""Decide whether a verified ghost patch is worth interrupting the user for.

Through Phase 3, every ghost patch that verifies is surfaced identically —
there is no sense of which fixes are worth an interruption versus which
should sit quietly on disk until asked for. `InterruptionPolicy` closes
that gap with a small, fully deterministic scoring model:

- patches are grouped by `fingerprint_for_patch`, built from the checks
  that originally failed (a failing targeted test and a lint failure are
  different "kinds" of fix, and the user may feel differently about each);
- with no recorded history for a fingerprint — or too little of it — the
  policy stays silent. That's the bias: never interrupt on a kind of fix
  it hasn't seen enough signal about yet;
- once there's enough history, the score is `(accepts - rejects) / total`
  for that fingerprint. `ignore` signals count toward the sample size (so a
  pattern the user has seen several times and never acted on eventually
  earns a verdict) but don't push the score itself either way — an ignore
  is not read as a weak accept or a weak reject.
"""

from __future__ import annotations

from dataclasses import dataclass

from shadowci.events import GhostPatch, Signal
from shadowci.signals import SignalStore

DEFAULT_THRESHOLD = 0.34
DEFAULT_MIN_SIGNALS = 2


def fingerprint_for_patch(patch: GhostPatch) -> str:
    """A stable key for "this kind of fix", used to look up signal history."""
    checks = sorted(patch.evidence.originally_failed_checks)
    return ",".join(checks) if checks else "(no-checks)"


@dataclass(frozen=True)
class InterruptionDecision:
    """The policy's verdict for a single ghost patch."""

    interrupt: bool
    score: float
    fingerprint: str
    sample_size: int


class InterruptionPolicy:
    """Scores a ghost patch's fingerprint against recorded signal history."""

    def __init__(
        self,
        store: SignalStore,
        threshold: float = DEFAULT_THRESHOLD,
        min_signals: int = DEFAULT_MIN_SIGNALS,
    ) -> None:
        self._store = store
        self._threshold = threshold
        self._min_signals = min_signals

    def evaluate(self, patch: GhostPatch) -> InterruptionDecision:
        fingerprint = fingerprint_for_patch(patch)
        return self.evaluate_fingerprint(fingerprint)

    def evaluate_fingerprint(self, fingerprint: str) -> InterruptionDecision:
        history = self._store.for_fingerprint(fingerprint)
        sample_size = len(history)

        if sample_size < self._min_signals:
            return InterruptionDecision(
                interrupt=False, score=0.0, fingerprint=fingerprint, sample_size=sample_size
            )

        accepts = sum(1 for record in history if record.signal is Signal.ACCEPT)
        rejects = sum(1 for record in history if record.signal is Signal.REJECT)
        score = (accepts - rejects) / sample_size

        return InterruptionDecision(
            interrupt=score >= self._threshold,
            score=score,
            fingerprint=fingerprint,
            sample_size=sample_size,
        )
