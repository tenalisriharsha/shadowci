"""Deterministic checks for issue #37: tell a reverted intentional change
apart from an accidental regression or a crash, with no model judgement.

The user's own edit is known ahead of any fix (`git diff HEAD` in the
shadow worktree, captured before the proposed fix is ever applied). Two
questions decide whether a verified fix is safe to auto-save:

- `is_crash_failure` — did the code even run? A crash (`NameError`,
  `ImportError`, `SyntaxError`, ...) means restoring the committed lines is
  simply correct, no matter what the diff "looks like" (issue #36).
- `fix_reverts_a_pure_addition` — did the user's edit only *add* lines,
  with nothing pre-existing removed or changed, and does the fix delete
  exactly what was added? That's the shape of an intentional behaviour
  change undone by the "natural" fix (issue #37's `average([])` example).
  A *modification* revert — the user changed an existing line, the fix
  changes it back — is the shape of an ordinary accidental regression
  (issue #35's off-by-one scenarios, or editing a different file than the
  one a failing test covers) and must keep behaving exactly as before.
"""

from __future__ import annotations

from shadowci.events import CheckResult

CRASH_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "NameError",
    "ImportError",
    "ModuleNotFoundError",
)


def is_crash_failure(failing_checks: tuple[CheckResult, ...]) -> bool:
    """Whether any failing check's output shows the code never ran at all,
    rather than running and giving a test the wrong answer."""
    for result in failing_checks:
        output = result.output
        lowered = output.lower()
        if "error collecting" in lowered or "error during collection" in lowered:
            return True
        if any(f"{marker}:" in output for marker in CRASH_MARKERS):
            return True
    return False


def diff_added_and_removed_lines(diff_text: str) -> tuple[set[str], set[str]]:
    """The content lines a unified diff adds and removes, headers excluded."""
    added: set[str] = set()
    removed: set[str] = set()
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.add(line[1:])
        elif line.startswith("-"):
            removed.add(line[1:])
    return added, removed


def fix_reverts_a_pure_addition(user_diff: str, fix_diff: str) -> bool:
    """Whether `user_diff` only added lines (touched nothing pre-existing)
    and `fix_diff` removes lines that `user_diff` added."""
    if not user_diff.strip() or not fix_diff.strip():
        return False
    user_added, user_removed = diff_added_and_removed_lines(user_diff)
    if not user_added or user_removed:
        return False
    _, fix_removed = diff_added_and_removed_lines(fix_diff)
    return bool(fix_removed & user_added)
