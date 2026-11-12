"""Shared prompt construction for model-backed `LLMProvider`s.

Both the headless `claude -p` provider and the local-model provider ask the
same two questions of a model — "write a regression test" and "propose a
fix" — so the prompt text lives in one place instead of being duplicated.

Issue #39: a prompt built from just the changed files' paths and the
failing check's output gives the model nothing to actually look at — no
diff, no file contents, no failing test source — so every fix is a guess,
and the model has no way to say "this looks like a deliberate change" other
than guessing at a diff that reverts it. `build_test_prompt`/`build_fix_prompt`
now also carry: the user's uncommitted edit as a unified diff, the current
contents of the changed files and of the failing test file(s) (each capped
and truncated with a visible marker), the #38 failure class and which test
files the user edited (never to be touched), and — on the fix prompt only —
an escape hatch for a deliberate change: replying with the single line
`INTENTIONAL: <reason>` instead of a diff.

Files that look sensitive (`.env*`, `*.pem`, `*.key`, or a name containing
"secret", "credential", or "token") are never read into a prompt, even when
they're part of the changeset.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

from shadowci.baseline import current_baseline_ref
from shadowci.classification import FailureClassification
from shadowci.events import ChangeSet, CheckResult
from shadowci.revert_check import failing_tests_by_file

_MAX_CONTENT_CHARS = 20_000
_TRUNCATION_MARKER = "\n...[truncated]...\n"

_SENSITIVE_NAME_GLOBS = (".env*", "*.pem", "*.key")
_SENSITIVE_NAME_FRAGMENTS = ("secret", "credential", "token")


def _looks_sensitive(relative_path: str) -> bool:
    name = Path(relative_path).name.lower()
    if any(fnmatch.fnmatch(name, pattern) for pattern in _SENSITIVE_NAME_GLOBS):
        return True
    return any(fragment in name for fragment in _SENSITIVE_NAME_FRAGMENTS)


def _repo_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _truncate(content: str) -> str:
    if len(content) <= _MAX_CONTENT_CHARS:
        return content
    return content[:_MAX_CONTENT_CHARS] + _TRUNCATION_MARKER


def _describe_failures(check_results: tuple[CheckResult, ...]) -> str:
    failing = [result for result in check_results if not result.passed]
    if not failing:
        return "(no failing checks provided)"
    return "\n".join(f"- {result.name}: {result.output.strip()}" for result in failing)


def _diff_for_paths(worktree_root: Path, relative_paths: list[str], ref: str) -> str:
    """`git diff <ref>` restricted to `relative_paths` — never asks git
    about a sensitive file, so one can never show up in the resulting
    text. `ref` is issue #52's baseline snapshot when one exists, `HEAD`
    otherwise — see `shadowci.baseline.current_baseline_ref`."""
    if not relative_paths:
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", ref, "--", *relative_paths],
            cwd=worktree_root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return ""
    return result.stdout


def _file_contents_section(worktree_root: Path, relative_paths: list[str]) -> str:
    blocks = []
    for relative in relative_paths:
        file_path = worktree_root / relative
        if not file_path.is_file():
            continue
        content = _truncate(file_path.read_text(errors="replace"))
        blocks.append(f"--- {relative} ---\n{content}")
    return "\n\n".join(blocks)


def _classification_section(classification: FailureClassification | None) -> str:
    if classification is None:
        return ""
    parts = []
    if classification.failure_class is not None:
        parts.append(f"Failure class (see issue #38): {classification.failure_class.value}")
    if classification.edited_test_files:
        files = ", ".join(classification.edited_test_files)
        parts.append(
            f"You edited these test file(s) yourself: {files}. They are the new "
            "spec — never modify, edit, or touch them in your answer."
        )
    return "\n".join(parts)


def _shared_context(
    changeset: ChangeSet,
    check_results: tuple[CheckResult, ...],
    worktree_root: Path | None,
    project_root: Path | None,
    classification: FailureClassification | None,
) -> str:
    if project_root is not None:
        relative_paths = [_repo_relative(path, project_root) for path in changeset.paths]
    else:
        relative_paths = [str(path) for path in changeset.paths]
    visible_relative_paths = [path for path in relative_paths if not _looks_sensitive(path)]

    failing_test_files = sorted(failing_tests_by_file(check_results))
    visible_test_files = [path for path in failing_test_files if not _looks_sensitive(path)]

    diff = ""
    changed_contents = ""
    test_contents = ""
    if worktree_root is not None:
        ref = (
            current_baseline_ref(worktree_root, project_root)
            if project_root is not None
            else "HEAD"
        )
        diff = _diff_for_paths(worktree_root, visible_relative_paths, ref)
        changed_contents = _file_contents_section(worktree_root, visible_relative_paths)
        test_contents = _file_contents_section(
            worktree_root,
            [path for path in visible_test_files if path not in visible_relative_paths],
        )

    paths_line = ", ".join(relative_paths) or "(no files)"
    sections = [
        (
            "A deterministic check just failed in a shadow git worktree after "
            f"these files changed: {paths_line}"
        ),
        f"Failing checks:\n{_describe_failures(check_results)}",
    ]
    if diff:
        sections.append(f"The user's uncommitted edit, as a unified diff:\n{diff}")
    if changed_contents:
        sections.append(f"Current contents of the changed file(s):\n{changed_contents}")
    if test_contents:
        sections.append(f"Current contents of the failing test file(s):\n{test_contents}")
    classification_text = _classification_section(classification)
    if classification_text:
        sections.append(classification_text)
    return "\n\n".join(sections)


def build_test_prompt(
    changeset: ChangeSet,
    check_results: tuple[CheckResult, ...],
    worktree_root: Path | None,
    project_root: Path | None,
    *,
    classification: FailureClassification | None = None,
) -> str:
    """Prompt asking the model for a single regression test that reproduces the bug."""
    context = _shared_context(
        changeset, check_results, worktree_root, project_root, classification
    )
    return (
        f"{context}\n\n"
        "Write a single pytest regression test that reproduces this failure. "
        "Return only the test source code, no commentary."
    )


def build_fix_prompt(
    changeset: ChangeSet,
    check_results: tuple[CheckResult, ...],
    worktree_root: Path | None,
    project_root: Path | None,
    *,
    classification: FailureClassification | None = None,
) -> str:
    """Prompt asking the model for a minimal unified-diff fix."""
    context = _shared_context(
        changeset, check_results, worktree_root, project_root, classification
    )
    return (
        f"{context}\n\n"
        "Propose a minimal fix as a unified diff that would make the failing "
        "check pass without changing unrelated behavior. Return only the diff, "
        "no commentary.\n\n"
        "If this failure looks like a deliberate behavior change rather than a "
        "bug — the kind of thing you would ask a human about instead of silently "
        "fixing — reply with exactly one line instead of a diff: "
        "INTENTIONAL: <short reason>."
    )
