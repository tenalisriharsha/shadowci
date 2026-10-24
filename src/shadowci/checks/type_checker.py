"""Runs `mypy` inside the shadow worktree, when it's installed.

`mypy` is an optional dependency: a project that doesn't use it shouldn't be
penalized for not having it installed, so a missing module is reported as a
trivially-passing, skipped check rather than a failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shadowci.checks.base import CheckRunner, is_module_missing, run_subprocess
from shadowci.events import CheckResult


class MypyRunner(CheckRunner):
    """Runs `python -m mypy` against the shadow worktree."""

    name = "mypy"

    def __init__(self, targets: list[str] | None = None) -> None:
        self._targets = list(targets) if targets else ["."]

    def run(self, worktree_path: Path) -> CheckResult:
        returncode, output, duration = run_subprocess(
            [sys.executable, "-m", "mypy", *self._targets], cwd=worktree_path
        )
        if returncode != 0 and is_module_missing(output):
            return CheckResult(
                name=self.name,
                passed=True,
                output="mypy not installed, skipped",
                duration_seconds=duration,
            )
        return CheckResult(
            name=self.name, passed=returncode == 0, output=output, duration_seconds=duration
        )
