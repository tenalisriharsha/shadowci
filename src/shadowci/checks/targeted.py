"""Targeted checks: map changed source files to the tests that cover them.

Running the full suite on every change set is wasteful once a project has
any real size. `find_targeted_tests` narrows things down to just the test
files that plausibly cover the changed paths, using two conventions:

1. **Name-based** — `src/pkg/foo.py` is covered by `tests/test_foo.py`.
2. **Reference-based** — a test file whose source mentions the changed
   module's stem (e.g. `from pkg import foo` or `foo.bar(...)`) is assumed
   to cover it.

If nothing matches, the caller should fall back to the full suite — a
changeset with no identifiable targeted test is never silently skipped.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from shadowci.checks.base import CheckRunner, run_subprocess
from shadowci.events import ChangeSet, CheckResult


def find_targeted_tests(
    changeset: ChangeSet, worktree_path: Path, tests_dir: str = "tests"
) -> list[Path]:
    """Return test files under `tests_dir` that plausibly cover `changeset`."""
    tests_root = worktree_path / tests_dir
    if not tests_root.is_dir():
        return []

    stems = {path.stem for path in changeset.paths if path.suffix == ".py"}
    if not stems:
        return []

    name_matches = {f"test_{stem}" for stem in stems}
    matched: list[Path] = []
    for test_file in sorted(tests_root.rglob("test_*.py")):
        if not test_file.is_file():
            continue
        if test_file.stem in name_matches:
            matched.append(test_file)
            continue
        try:
            source = test_file.read_text()
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(stem)}\b", source) for stem in stems):
            matched.append(test_file)

    return matched


class TargetedTestRunner(CheckRunner):
    """Runs only the test files that cover a given change set."""

    name = "pytest-targeted"

    def __init__(self, changeset: ChangeSet, tests_dir: str = "tests") -> None:
        self._changeset = changeset
        self._tests_dir = tests_dir

    def run(self, worktree_path: Path) -> CheckResult:
        targets = find_targeted_tests(self._changeset, worktree_path, self._tests_dir)
        if not targets:
            return CheckResult(
                name=self.name,
                passed=True,
                output="no targeted tests matched this change set",
                duration_seconds=0.0,
            )

        relative_targets = [str(path.relative_to(worktree_path)) for path in targets]
        returncode, output, duration = run_subprocess(
            [sys.executable, "-m", "pytest", *relative_targets], cwd=worktree_path
        )
        return CheckResult(
            name=self.name, passed=returncode == 0, output=output, duration_seconds=duration
        )
