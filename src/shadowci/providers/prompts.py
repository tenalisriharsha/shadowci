"""Shared prompt construction for model-backed `LLMProvider`s.

Both the headless `claude -p` provider and the local-model provider ask the
same two questions of a model — "write a regression test" and "propose a
fix" — so the prompt text lives in one place instead of being duplicated.
"""

from __future__ import annotations

from shadowci.events import ChangeSet, CheckResult


def _describe_failures(check_results: tuple[CheckResult, ...]) -> str:
    failing = [result for result in check_results if not result.passed]
    if not failing:
        return "(no failing checks provided)"
    return "\n".join(f"- {result.name}: {result.output.strip()}" for result in failing)


def build_test_prompt(changeset: ChangeSet, check_results: tuple[CheckResult, ...]) -> str:
    """Prompt asking the model for a single regression test that reproduces the bug."""
    paths = ", ".join(str(path) for path in changeset.paths)
    return (
        "A deterministic check just failed in a shadow git worktree after these "
        f"files changed: {paths}\n\nFailing checks:\n{_describe_failures(check_results)}\n\n"
        "Write a single pytest regression test that reproduces this failure. "
        "Return only the test source code, no commentary."
    )


def build_fix_prompt(changeset: ChangeSet, check_results: tuple[CheckResult, ...]) -> str:
    """Prompt asking the model for a minimal unified-diff fix."""
    paths = ", ".join(str(path) for path in changeset.paths)
    return (
        "A deterministic check just failed in a shadow git worktree after these "
        f"files changed: {paths}\n\nFailing checks:\n{_describe_failures(check_results)}\n\n"
        "Propose a minimal fix as a unified diff that would make the failing "
        "check pass without changing unrelated behavior. Return only the diff, "
        "no commentary."
    )
