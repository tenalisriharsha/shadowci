"""Runs the project's `pytest` suite inside the shadow worktree."""

from __future__ import annotations

import sys
from pathlib import Path

from shadowci.checks.base import CheckRunner, run_subprocess_with_timeout
from shadowci.config import DEFAULT_CHECK_TIMEOUT_SECONDS
from shadowci.events import CheckResult

_NO_TESTS_COLLECTED = 5


class PytestRunner(CheckRunner):
    """Runs `python -m pytest` against the shadow worktree."""

    name = "pytest"

    def __init__(
        self,
        args: list[str] | None = None,
        timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._args = list(args) if args else []
        self._timeout_seconds = timeout_seconds

    def run(self, worktree_path: Path) -> CheckResult:
        returncode, output, duration, timed_out = run_subprocess_with_timeout(
            [sys.executable, "-m", "pytest", *self._args],
            worktree_path,
            timeout_seconds=self._timeout_seconds,
        )
        if timed_out:
            return CheckResult(
                name=self.name,
                passed=False,
                output=f"timed out after {self._timeout_seconds:g}s",
                duration_seconds=duration,
                timed_out=True,
            )
        if returncode == _NO_TESTS_COLLECTED:
            return CheckResult(
                name=self.name,
                passed=True,
                output="no tests collected, nothing to check",
                duration_seconds=duration,
            )
        return CheckResult(
            name=self.name, passed=returncode == 0, output=output, duration_seconds=duration
        )
