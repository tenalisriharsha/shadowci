"""The LLM provider interface.

ShadowCI only ever escalates to an `LLMProvider` after deterministic checks
(tests, type checker, linters — wired up in phase 2) have already found a
real failure. A provider's job is narrow: given a `ChangeSet` and the
`CheckResult`s that failed, write a regression test that reproduces the bug
and propose a fix as a unified diff. Everything else — running the fix,
confirming no regressions, building the `GhostPatch` — stays in ShadowCI's
core pipeline so every provider is interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from shadowci.events import ChangeSet, CheckResult


class LLMProvider(ABC):
    """A pluggable backend for writing regression tests and proposing fixes."""

    name: str

    @abstractmethod
    def write_regression_test(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        """Return source for a test that reproduces the observed failure."""
        raise NotImplementedError

    @abstractmethod
    def propose_fix(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        """Return a unified diff that fixes the failure in the shadow worktree."""
        raise NotImplementedError
