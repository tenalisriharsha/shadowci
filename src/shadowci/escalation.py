"""Decide when to escalate a change set to an `LLMProvider`.

ShadowCI only ever calls an LLM after deterministic checks (tests, type
checker, linter) have already found a real failure — a clean check run
never triggers a model call, keeping the common case free and silent.
"""

from __future__ import annotations

from dataclasses import dataclass

from shadowci.events import ChangeSet, CheckResult
from shadowci.providers.base import LLMProvider


@dataclass(frozen=True)
class EscalationResult:
    """What an `LLMProvider` produced after a real failure was confirmed."""

    failing_checks: tuple[CheckResult, ...]
    regression_test: str
    proposed_fix: str


class EscalationPolicy:
    """Calls into an `LLMProvider` only when deterministic checks fail."""

    def __init__(self, provider: LLMProvider) -> None:
        self._provider = provider

    def maybe_escalate(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> EscalationResult | None:
        """Escalate to the provider if anything in `check_results` failed, else None."""
        failing = tuple(result for result in check_results if not result.passed)
        if not failing:
            return None

        test = self._provider.write_regression_test(changeset, failing)
        fix = self._provider.propose_fix(changeset, failing)
        return EscalationResult(failing_checks=failing, regression_test=test, proposed_fix=fix)
