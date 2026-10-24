"""Runs the project's `pytest` suite inside the shadow worktree."""

from __future__ import annotations

import sys
from pathlib import Path

from shadowci.checks.base import CheckRunner, run_subprocess
from shadowci.events import CheckResult

_NO_TESTS_COLLECTED = 5


class PytestRunner(CheckRunner):
    """Runs `python -m pytest` against the shadow worktree."""

    name = "pytest"

    def __init__(self, args: list[str] | None = None) -> None:
        self._args = list(args) if args else []

    def run(self, worktree_path: Path) -> CheckResult:
        returncode, output, duration = run_subprocess(
            [sys.executable, "-m", "pytest", *self._args], cwd=worktree_path
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
