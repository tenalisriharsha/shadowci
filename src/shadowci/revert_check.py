"""Issue #37: don't silently save a fix that puts back the user's own edit.

`EscalationPolicy` only ever asks a provider to "propose a minimal fix" —
when the user changed behavior on purpose and an old test now fails, the
model's "natural" fix typically undoes exactly that edit. The shadow
worktree's `git diff HEAD`, taken before any fix is applied, *is* the
user's own edit, so whether a proposed fix puts it back is deterministic:
no model judgement involved, just set arithmetic on two diffs' added and
removed lines (`shadowci.diffing.fix_reverts_user_change`).

Two things keep this from firing on an ordinary bug fix:

- **crash-type failures are exempt** (`is_crash_failure`) — issue #36's
  guard. Restoring HEAD's lines is simply correct when the code couldn't
  even import.
- **the pipeline must be able to offer a real second option.** A fix that
  reverts the user's edit is only turned into a question when
  `build_update_test_patch` can deterministically rewrite the failing
  test(s) to match the user's new behavior and verify that rewrite in the
  shadow worktree. Plenty of genuine, accidental bugs (an off-by-one, a
  flipped operator, an inverted condition) also happen to produce a fix
  that structurally reverts the user's edit — the only distinguishing
  signal available here is whether the failing test is something we can
  actually rewrite (today: a `pytest.raises(...)` block that no longer
  raises). When it isn't, there's no deterministic question to ask, so the
  pipeline falls back to the normal, silent, verified fix.
"""

from __future__ import annotations

import ast
import difflib
import re
import subprocess
from pathlib import Path

from shadowci.checks.base import clear_bytecode_cache
from shadowci.checks.lint_runner import RuffRunner
from shadowci.checks.pytest_runner import PytestRunner
from shadowci.checks.suite import run_checks
from shadowci.checks.type_checker import MypyRunner
from shadowci.events import ChangeSet, CheckResult, EvidenceCard, GhostPatch
from shadowci.shadow_worktree import ShadowWorktree

CRASH_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "NameError",
    "ImportError",
    "ModuleNotFoundError",
)

_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def is_crash_failure(failing_checks: tuple[CheckResult, ...]) -> bool:
    """Whether any failing pytest-family check looks like a crash (the code
    couldn't even be imported/collected) rather than a plain assertion
    failure — issue #36's exemption."""
    for result in failing_checks:
        if not result.name.startswith("pytest"):
            continue
        text = result.output
        if any(marker in text for marker in CRASH_MARKERS):
            return True
        lowered = text.lower()
        if "error collecting" in lowered or "error during collection" in lowered:
            return True
    return False


def diff_head(worktree_path: Path) -> str:
    """The shadow worktree's uncommitted changes right now — the user's
    own edit, as long as this is called before any fix is applied."""
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def failing_tests_by_file(failing_checks: tuple[CheckResult, ...]) -> dict[str, set[str]]:
    """`{test_relpath: {test_function_name, ...}}` for every pytest node id
    pytest reported as FAILED (a collection-wide ERROR has no `::name` and
    is skipped — crash failures are handled separately)."""
    by_file: dict[str, set[str]] = {}
    for result in failing_checks:
        if not result.name.startswith("pytest"):
            continue
        for node_id in _FAILED_NODE_RE.findall(result.output):
            if "::" not in node_id:
                continue
            file_part, test_name = node_id.split("::", 1)
            by_file.setdefault(file_part, set()).add(test_name)
    return by_file


def _is_pytest_raises(node: ast.With) -> bool:
    if len(node.items) != 1:
        return False
    expr = node.items[0].context_expr
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Attribute):
        return func.attr == "raises"
    if isinstance(func, ast.Name):
        return func.id == "raises"
    return False


def _raises_blocks_to_unwrap(tree: ast.Module, test_names: set[str]) -> list[ast.With]:
    blocks = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in test_names:
            for stmt in node.body:
                if isinstance(stmt, ast.With) and _is_pytest_raises(stmt):
                    blocks.append(stmt)
    return blocks


def _dedent_lines(lines: list[str], spaces: int) -> list[str]:
    stripped = []
    for line in lines:
        if line.strip() == "":
            stripped.append(line)
        else:
            stripped.append(line[spaces:] if line[:spaces].isspace() else line.lstrip())
    return stripped


def _unwrap_raises_blocks(source: str, blocks: list[ast.With]) -> str:
    """Replace each `with pytest.raises(...):` block with its own
    (dedented) body, editing only those line ranges and leaving every
    other line of `source` byte-for-byte untouched."""
    lines = source.splitlines(keepends=True)
    for block in sorted(blocks, key=lambda b: b.lineno, reverse=True):
        start = block.lineno - 1
        end = block.end_lineno
        body_start = block.body[0].lineno - 1
        body_end = block.body[-1].end_lineno
        indent_diff = block.body[0].col_offset - block.col_offset
        body_lines = _dedent_lines(lines[body_start:body_end], indent_diff)
        lines[start:end] = body_lines
    return "".join(lines)


def _pytest_module_still_used(tree: ast.Module) -> bool:
    return any(isinstance(node, ast.Name) and node.id == "pytest" for node in ast.walk(tree))


def _drop_bare_pytest_import(source: str) -> str:
    """Remove a standalone `import pytest` line once nothing in the
    rewritten test references `pytest` anymore (an unused import would
    otherwise fail the linter)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source
    if _pytest_module_still_used(tree):
        return source

    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Import)
            and len(node.names) == 1
            and node.names[0].name == "pytest"
            and node.names[0].asname is None
        ):
            del lines[node.lineno - 1 : node.end_lineno]
            break
    return "".join(lines)


def _rewrite_test_source(source: str, test_names: set[str]) -> str | None:
    """A deterministic rewrite of `source` that no longer expects the
    exception(s) `test_names` currently fail to raise, or None when no
    such rewrite applies."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    blocks = _raises_blocks_to_unwrap(tree, test_names)
    if not blocks:
        return None
    rewritten = _unwrap_raises_blocks(source, blocks)
    return _drop_bare_pytest_import(rewritten)


def _make_diff(relative: str, before: str, after: str) -> str:
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    text = "".join(diff_lines)
    return text if text.endswith("\n") else text + "\n"


def build_update_test_patch(
    worktree: ShadowWorktree,
    changeset: ChangeSet,
    originally_failing: tuple[CheckResult, ...],
) -> GhostPatch | None:
    """Try to build a verified patch that edits the failing test file(s) so
    they match the user's current behavior instead of the old expectation.

    Returns None when no deterministic rewrite applies, or the rewrite
    doesn't actually make the suite pass — the caller should fall back to
    the normal fix flow in that case. The shadow worktree is left exactly
    as it was found either way: the edited test file(s) are restored
    before returning.
    """
    by_file = failing_tests_by_file(originally_failing)
    if not by_file:
        return None

    shadow_path = worktree.path
    diffs: list[str] = []
    originals: dict[Path, str] = {}

    try:
        for relative, test_names in sorted(by_file.items()):
            test_path = shadow_path / relative
            if not test_path.is_file():
                return None
            original = test_path.read_text()
            rewritten = _rewrite_test_source(original, test_names)
            if rewritten is None or rewritten == original:
                return None
            originals[test_path] = original
            diffs.append(_make_diff(relative, original, rewritten))
            test_path.write_text(rewritten)

        clear_bytecode_cache(shadow_path)
        results = run_checks([PytestRunner(), MypyRunner(), RuffRunner()], shadow_path)
    except Exception:  # noqa: BLE001 - any failure here just means "no question to ask"
        return None
    finally:
        for test_path, original in originals.items():
            test_path.write_text(original)
        clear_bytecode_cache(shadow_path)

    failing_after = tuple(result for result in results if not result.passed)
    if failing_after:
        return None

    file_names = ", ".join(str(path.name) for path in changeset.paths) or "(no files)"
    evidence = EvidenceCard(
        summary=f"updates the failing test to match {file_names}'s new behavior",
        failing_test_before="\n".join(originals.values()),
        check_results=results,
        originally_failed_checks=tuple(result.name for result in originally_failing),
    )
    return GhostPatch(diff="".join(diffs), evidence=evidence, changeset=changeset)
