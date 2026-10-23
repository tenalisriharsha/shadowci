"""A deterministic, network-free provider for tests, CI, and demos.

`ScriptedProvider` never calls out to a network or a model — it returns
canned responses from a script supplied by the caller (or a fixed default),
cycling through entries as calls are made. This keeps the pipeline fully
testable and runnable with no API key, and doubles as the reference
implementation every other provider's behavior is checked against.
"""

from __future__ import annotations

from shadowci.events import ChangeSet, CheckResult
from shadowci.providers.base import LLMProvider

_DEFAULT_TEST = (
    "def test_shadowci_regression():\n"
    "    raise AssertionError('scripted provider: no real check failed yet')\n"
)
_DEFAULT_FIX = "--- a/placeholder\n+++ b/placeholder\n"


class ScriptedProvider(LLMProvider):
    """Returns pre-scripted, deterministic responses — no model call."""

    name = "scripted"

    def __init__(
        self,
        tests: list[str] | None = None,
        fixes: list[str] | None = None,
    ) -> None:
        self._tests = list(tests) if tests else [_DEFAULT_TEST]
        self._fixes = list(fixes) if fixes else [_DEFAULT_FIX]
        self._test_calls = 0
        self._fix_calls = 0

    def write_regression_test(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        test = self._tests[min(self._test_calls, len(self._tests) - 1)]
        self._test_calls += 1
        return test

    def propose_fix(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        fix = self._fixes[min(self._fix_calls, len(self._fixes) - 1)]
        self._fix_calls += 1
        return fix
