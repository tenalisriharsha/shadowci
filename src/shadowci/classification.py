"""Issue #38: classify a failing check before it's escalated.

Through issue #37, every verified fix is grouped for the interruption
policy by the *check* that failed and nothing else — a patch that silently
reverts the user's own edit and a patch that fixes a genuine regression
elsewhere look identical as long as the same check failed. `classify_failure`
sorts a failure into one of four classes before the pipeline escalates it,
using what it already knows: which files the user edited, which test
failed, and whether that test passed at `HEAD`.

- `test-edited` — one of the files the user just edited is itself the
  failing test file. The test is the new spec; a patch must never touch it.
- `pre-existing` — the failing test was already failing at `HEAD`, before
  this edit, so the failure isn't a regression of it at all.
- `unclear-behavior-change` — the user edited the code this test covers
  (by name or import — see `shadowci.checks.targeted`), and the test
  passed at `HEAD`: ambiguous, could be a bug or an on-purpose change.
- `regression-elsewhere` — the edited file isn't what this test covers,
  and the test passed at `HEAD`: a real regression, fix with confidence.

When no failing check is an identifiable pytest failure (a crash/collection
error, or a pure type-checker/linter failure), there's nothing to classify
and `failure_class` is `None` — the interruption policy's fingerprint falls
back to its pre-#38 plain `checks` form.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from shadowci.checks.targeted import find_targeted_tests
from shadowci.events import ChangeSet, CheckResult
from shadowci.head_check import node_ids_passed_at_head
from shadowci.revert_check import failing_tests_by_file


class FailureClass(str, Enum):
    REGRESSION_ELSEWHERE = "regression-elsewhere"
    TEST_EDITED = "test-edited"
    UNCLEAR_BEHAVIOR_CHANGE = "unclear-behavior-change"
    PRE_EXISTING = "pre-existing"


@dataclass(frozen=True)
class FailureClassification:
    """The verdict for one failed check run, plus the test file(s) (if
    any) the user directly edited that produced it — a patch must never
    touch one of those when `failure_class` is `TEST_EDITED`."""

    failure_class: FailureClass | None
    edited_test_files: tuple[str, ...] = ()


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def classify_failure(
    changeset: ChangeSet,
    failing_checks: tuple[CheckResult, ...],
    worktree_path: Path,
    project_root: Path,
    tests_dir: str = "tests",
) -> FailureClassification:
    by_file = failing_tests_by_file(failing_checks)
    if not by_file:
        return FailureClassification(failure_class=None)

    edited_relpaths = {_repo_relative(path, project_root) for path in changeset.paths}
    edited_test_files = tuple(sorted(edited_relpaths & by_file.keys()))
    if edited_test_files:
        return FailureClassification(
            failure_class=FailureClass.TEST_EDITED, edited_test_files=edited_test_files
        )

    node_ids = tuple(
        f"{file}::{name}" for file, names in sorted(by_file.items()) for name in sorted(names)
    )
    head_status = node_ids_passed_at_head(project_root, node_ids)
    if node_ids and not any(head_status.get(node_id, False) for node_id in node_ids):
        return FailureClassification(failure_class=FailureClass.PRE_EXISTING)

    covered_test_files = {
        _repo_relative(path, worktree_path)
        for path in find_targeted_tests(changeset, worktree_path, tests_dir)
    }
    if set(by_file) & covered_test_files:
        return FailureClassification(failure_class=FailureClass.UNCLEAR_BEHAVIOR_CHANGE)
    return FailureClassification(failure_class=FailureClass.REGRESSION_ELSEWHERE)
