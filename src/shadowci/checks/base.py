"""Check runner interface.

A `CheckRunner` executes one deterministic check (tests, type checker,
linter, targeted tests, …) against the shadow worktree and reports a
`CheckResult`. Runners are always pointed at the shadow worktree's path —
never the live working tree — so a bad check can't touch what you're
editing.
"""

from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

from shadowci.events import CheckResult

MODULE_MISSING_MARKER = "No module named"


class CheckRunner(ABC):
    """A single deterministic check that can run against a worktree."""

    name: str

    @abstractmethod
    def run(self, worktree_path: Path) -> CheckResult:
        """Execute the check against `worktree_path` and report the result."""
        raise NotImplementedError


def run_subprocess(args: list[str], cwd: Path) -> tuple[int, str, float]:
    """Run `args` in `cwd`, returning (returncode, combined output, duration)."""
    start = time.monotonic()
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
        returncode = result.returncode
        output = result.stdout + result.stderr
    except FileNotFoundError as exc:
        returncode = 127
        output = str(exc)
    duration = time.monotonic() - start
    return returncode, output, duration


def is_module_missing(output: str) -> bool:
    """Whether `output` indicates `python -m <tool>` failed because the tool isn't installed."""
    return MODULE_MISSING_MARKER in output
