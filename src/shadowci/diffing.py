"""Set-based comparison of unified diffs' added/removed content lines.

Shared by the evaluation harness (scoring `reverted_user_change`, issue
#35) and the real pipeline's revert check (issue #37) so both agree on
what "this fix puts back the user's own edit" means.
"""

from __future__ import annotations


def diff_touched_files(diff_text: str) -> set[str]:
    """The `b/<path>` targets a unified diff's `+++` headers name — the
    files it would actually write to if applied, as opposed to whatever
    files happened to trigger the check run that produced it."""
    files: set[str] = set()
    for line in diff_text.splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[len("+++ ") :].strip()
        if target == "/dev/null":
            continue
        files.add(target.removeprefix("b/"))
    return files


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


def fix_reverts_user_change(user_diff: str, fix_diff: str) -> bool:
    """Whether `fix_diff` puts back lines `user_diff` changed: the fix adds
    back what the user removed, or removes what the user added (or both)."""
    if not user_diff.strip() or not fix_diff.strip():
        return False
    user_added, user_removed = diff_added_and_removed_lines(user_diff)
    fix_added, fix_removed = diff_added_and_removed_lines(fix_diff)
    return bool(fix_added & user_removed) or bool(fix_removed & user_added)
