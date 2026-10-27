"""Phase 3: turn a verified escalation into an applyable `GhostPatch`.

`EscalationPolicy` only asks an `LLMProvider` for a regression test and a
fix — it never runs either. `GhostPatchBuilder` is where that gets made
real, entirely inside the shadow worktree:

1. write the regression test into the shadow worktree and run it — it must
   fail, or it didn't actually reproduce the bug and nothing proceeds;
2. apply the proposed fix with `git apply`;
3. re-run the full suite (tests + type checker + linter) to confirm the fix
   works and nothing else regressed;
4. assemble a single unified diff (the fix plus the new test file) that can
   later be re-applied to the live tree with `git apply`, which naturally
   rebases onto whatever the user edited elsewhere in the meantime — it
   only fails if the user's own edits overlap the exact lines being patched.

The live working tree is never touched by `build()` — only `apply_to_live_tree`
writes there, and only when explicitly called.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from shadowci.checks.base import CheckRunner
from shadowci.checks.lint_runner import RuffRunner
from shadowci.checks.pytest_runner import PytestRunner
from shadowci.checks.suite import run_checks
from shadowci.checks.type_checker import MypyRunner
from shadowci.events import ChangeSet, CheckResult, EvidenceCard, GhostPatch
from shadowci.shadow_worktree import ShadowWorktree

GHOST_TESTS_SUBDIR = "tests"


class GhostPatchError(RuntimeError):
    """Raised when a ghost patch cannot be verified or applied."""


def _ghost_test_filename(changeset: ChangeSet) -> str:
    digest = hashlib.sha1(
        "|".join(sorted(str(path) for path in changeset.paths)).encode("utf-8")
    ).hexdigest()[:10]
    return f"test_shadowci_ghost_{digest}.py"


def _run_git_apply(diff: str, cwd: Path, check_only: bool) -> subprocess.CompletedProcess:
    args = ["git", "apply"]
    if check_only:
        args.append("--check")
    args.append("-")
    return subprocess.run(args, cwd=cwd, input=diff, capture_output=True, text=True)


def _new_file_diff(relative_path: Path, content: str) -> str:
    """A git-apply-compatible unified diff that creates `relative_path`."""
    rel = relative_path.as_posix()
    lines = content.splitlines()
    header = [
        f"diff --git a/{rel} b/{rel}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{rel}",
        f"@@ -0,0 +1,{len(lines)} @@",
    ]
    body = [f"+{line}" for line in lines]
    return "\n".join(header + body) + "\n"


def _summarize(changeset: ChangeSet, failing_checks: tuple[CheckResult, ...]) -> str:
    files = ", ".join(str(path.name) for path in changeset.paths) or "(no files)"
    checks = ", ".join(result.name for result in failing_checks) or "(none)"
    return f"verified fix for {files} — originally failed: {checks}"


class GhostPatchBuilder:
    """Verifies a regression test + fix inside the shadow worktree, then
    assembles a `GhostPatch` ready to apply to the live tree."""

    def __init__(self, worktree: ShadowWorktree, tests_dir: str = GHOST_TESTS_SUBDIR) -> None:
        self._worktree = worktree
        self._tests_dir = tests_dir

    def build(
        self,
        changeset: ChangeSet,
        failing_checks: tuple[CheckResult, ...],
        regression_test: str,
        proposed_fix: str,
    ) -> GhostPatch:
        shadow_path = self._worktree.path
        test_relative = Path(self._tests_dir) / _ghost_test_filename(changeset)
        test_path = shadow_path / test_relative

        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(regression_test)

        try:
            reproduction = PytestRunner(args=[test_relative.as_posix()]).run(shadow_path)
            if reproduction.passed:
                raise GhostPatchError(
                    "regression test did not reproduce the failure — refusing to "
                    "build a ghost patch from an unverified test"
                )

            check = _run_git_apply(proposed_fix, shadow_path, check_only=True)
            if check.returncode != 0:
                raise GhostPatchError(
                    f"proposed fix did not apply cleanly in the shadow worktree: "
                    f"{check.stderr.strip()}"
                )
            applied = _run_git_apply(proposed_fix, shadow_path, check_only=False)
            if applied.returncode != 0:
                raise GhostPatchError(
                    f"proposed fix failed to apply: {applied.stderr.strip()}"
                )

            confirmation = self._confirm_no_regressions(shadow_path)
        except GhostPatchError:
            test_path.unlink(missing_ok=True)
            raise

        diff = self._assemble_diff(proposed_fix, test_relative, regression_test)
        evidence = EvidenceCard(
            summary=_summarize(changeset, failing_checks),
            failing_test_before=regression_test,
            check_results=confirmation,
            originally_failed_checks=tuple(result.name for result in failing_checks),
        )
        return GhostPatch(diff=diff, evidence=evidence, changeset=changeset)

    def _confirm_no_regressions(self, shadow_path: Path) -> tuple[CheckResult, ...]:
        runners: list[CheckRunner] = [PytestRunner(), MypyRunner(), RuffRunner()]
        results = run_checks(runners, shadow_path)
        failing = tuple(result for result in results if not result.passed)
        if failing:
            names = ", ".join(result.name for result in failing)
            raise GhostPatchError(
                f"fix applied but the full suite still fails ({names}):\n"
                + "\n".join(result.output for result in failing)
            )
        return results

    def _assemble_diff(
        self, proposed_fix: str, test_relative: Path, regression_test: str
    ) -> str:
        fix_diff = proposed_fix if proposed_fix.endswith("\n") else proposed_fix + "\n"
        test_diff = _new_file_diff(test_relative, regression_test)
        return fix_diff + test_diff


def check_applies_to_live_tree(diff: str, live_root: Path) -> bool:
    """Whether `diff` would still apply cleanly onto `live_root` right now."""
    result = _run_git_apply(diff, live_root, check_only=True)
    return result.returncode == 0


def apply_to_live_tree(diff: str, live_root: Path) -> None:
    """Apply a verified ghost patch diff to the live working tree, one keystroke.

    Rebases implicitly: `git apply` matches on the diff's context lines, so
    unrelated edits made in the live tree since the patch was built don't
    block it — only edits that overlap the exact lines being patched do,
    and those raise instead of silently clobbering the user's work.
    """
    check = _run_git_apply(diff, live_root, check_only=True)
    if check.returncode != 0:
        raise GhostPatchError(
            "ghost patch no longer applies cleanly — the live tree has changed "
            f"too much since this fix was verified: {check.stderr.strip()}"
        )
    applied = _run_git_apply(diff, live_root, check_only=False)
    if applied.returncode != 0:
        raise GhostPatchError(f"failed to apply ghost patch: {applied.stderr.strip()}")
