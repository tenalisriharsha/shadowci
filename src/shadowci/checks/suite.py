"""Pick and run the right deterministic checks for a change set.

Cheapest/most-targeted checks run first: if a changed file maps to a
specific test file, only that test file runs; otherwise the full suite
runs so coverage is never silently skipped. Type checking and linting run
after, both degrading to a skipped pass when the tool isn't installed.
"""

from __future__ import annotations

from pathlib import Path

from shadowci.checks.base import CheckRunner
from shadowci.checks.lint_runner import RuffRunner
from shadowci.checks.pytest_runner import PytestRunner
from shadowci.checks.targeted import TargetedTestRunner, find_targeted_tests
from shadowci.checks.type_checker import MypyRunner
from shadowci.events import ChangeSet, CheckResult


def build_check_runners(
    changeset: ChangeSet, worktree_path: Path, tests_dir: str = "tests"
) -> list[CheckRunner]:
    """Choose check runners for `changeset`: targeted tests when possible, else all."""
    targets = find_targeted_tests(changeset, worktree_path, tests_dir)
    test_runner: CheckRunner = (
        TargetedTestRunner(changeset, tests_dir) if targets else PytestRunner()
    )
    return [test_runner, MypyRunner(), RuffRunner()]


def run_checks(
    runners: list[CheckRunner], worktree_path: Path
) -> tuple[CheckResult, ...]:
    """Run every runner in `runners` against `worktree_path`, in order."""
    return tuple(runner.run(worktree_path) for runner in runners)
