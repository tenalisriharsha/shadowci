"""Decide whether a verified ghost patch is worth interrupting the user for.

Through Phase 3, every ghost patch that verifies is surfaced identically —
there is no sense of which fixes are worth an interruption versus which
should sit quietly on disk until asked for. `InterruptionPolicy` closes
that gap with a small, fully deterministic scoring model:

- patches are grouped by `fingerprint_for_patch`, built from the checks
  that originally failed (a failing targeted test and a lint failure are
  different "kinds" of fix, and the user may feel differently about each)
  plus, since issue #38, the failure's classification (a patch that
  reverts a user's own edit and a patch that fixes a genuine regression
  elsewhere no longer share history just because the same check failed);
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


def fingerprint_for_patch(patch: GhostPatch, failure_class: str | None = None) -> str:
    """A stable key for "this kind of fix", used to look up signal history.

    `failure_class` (issue #38's `regression-elsewhere` / `test-edited` /
    `unclear-behavior-change` / `pre-existing`) is prefixed on when known,
    so two failures that happen to trip the same check but were classified
    differently never share one fingerprint. Left `None` — the only way a
    fingerprint was ever built before issue #38, and still how it's built
    when nothing could be classified — it falls back to the plain,
    unprefixed form, so a signals file written by an earlier version keeps
    loading as a legacy fingerprint alongside any new, prefixed ones.
    """
    checks = sorted(patch.evidence.originally_failed_checks)
    base = ",".join(checks) if checks else "(no-checks)"
    return f"{failure_class}:{base}" if failure_class else base


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
