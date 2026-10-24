"""Runs `ruff` inside the shadow worktree, when it's installed.

Same skip-not-fail behavior as `MypyRunner`: a project without `ruff`
installed shouldn't have its checks fail just because the linter is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

from shadowci.checks.base import CheckRunner, is_module_missing, run_subprocess
from shadowci.events import CheckResult


class RuffRunner(CheckRunner):
    """Runs `python -m ruff check` against the shadow worktree."""

    name = "ruff"

    def __init__(self, targets: list[str] | None = None) -> None:
        self._targets = list(targets) if targets else ["."]

    def run(self, worktree_path: Path) -> CheckResult:
        returncode, output, duration = run_subprocess(
            [sys.executable, "-m", "ruff", "check", *self._targets], cwd=worktree_path
        )
        if returncode != 0 and is_module_missing(output):
            return CheckResult(
                name=self.name,
                passed=True,
                output="ruff not installed, skipped",
                duration_seconds=duration,
            )
        return CheckResult(
            name=self.name, passed=returncode == 0, output=output, duration_seconds=duration
        )
