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
import json
import re
import subprocess
import sys
import tempfile
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


def diff_head_reversed(worktree_path: Path) -> str:
    """The exact inverse of `diff_head`: a patch that would put the
    worktree's uncommitted edit back to HEAD's content.

    Issue #39: when a provider declines to propose a fix (an `INTENTIONAL:`
    reply), this is the deterministic stand-in for "keep the old
    behaviour" — there's no model-authored diff to apply, but reverting the
    user's own edit is exactly what `git diff -R HEAD` already computes.
    """
    result = subprocess.run(
        ["git", "diff", "-R", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


INTENTIONAL_PREFIX = "INTENTIONAL:"


def is_intentional_reply(proposed_fix: str) -> bool:
    """Issue #39: a provider may answer a fix request with the literal line
    `INTENTIONAL: <reason>` instead of a diff, when it judges the failure to
    be a deliberate behavior change rather than a bug."""
    return proposed_fix.strip().startswith(INTENTIONAL_PREFIX)


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


def _raises_blocks_by_function(
    tree: ast.Module, test_names: set[str]
) -> dict[str, list[ast.With]]:
    """`{function_name: [with-blocks]}` for every `pytest.raises(...)`
    block directly in the body of a function named in `test_names` that
    actually has one (functions with none are left out)."""
    by_function: dict[str, list[ast.With]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in test_names:
            blocks = [
                stmt
                for stmt in node.body
                if isinstance(stmt, ast.With) and _is_pytest_raises(stmt)
            ]
            if blocks:
                by_function[node.name] = blocks
    return by_function


def _single_statement_source(source: str, block: ast.With) -> str | None:
    """The exact source text of `block`'s one and only statement, when it
    is a bare expression statement — or None when the block holds several
    statements (ambiguous: which one's result would the assertion even
    be about?) or something other than an expression (no result to
    assert)."""
    if len(block.body) != 1:
        return None
    stmt = block.body[0]
    if not isinstance(stmt, ast.Expr):
        return None
    return ast.get_source_segment(source, stmt)


_PROBE_SCRIPT = """
import ast
import json
import sys

sys.path.insert(0, sys.argv[1])
module_path = sys.argv[2]
expr_source = sys.argv[3]
output_path = sys.argv[4]

namespace = {"__name__": "__shadowci_probe__", "__file__": module_path}

result = {"status": "raised"}
try:
    with open(module_path, "r", encoding="utf-8") as handle:
        module_source = handle.read()
    exec(compile(module_source, module_path, "exec"), namespace)
    value = eval(compile(expr_source, "<shadowci-probe>", "eval"), namespace)
except BaseException:
    result = {"status": "raised"}
else:
    try:
        literal_text = repr(value)
        if ast.literal_eval(literal_text) != value:
            raise ValueError("value does not round-trip through repr()")
    except Exception:
        result = {"status": "not_literal"}
    else:
        result = {"status": "ok", "repr": literal_text}

with open(output_path, "w", encoding="utf-8") as handle:
    json.dump(result, handle)
"""


def _probe_expression(
    worktree_path: Path, module_path: Path, expr_source: str
) -> dict[str, str] | None:
    """Evaluate `expr_source` in a fresh subprocess - never in this
    process - after running `module_path`'s own module-level code (so its
    imports are in scope) with `worktree_path` on `sys.path`.

    Returns `{"status": "ok", "repr": <text>}` when the expression
    evaluates to a value whose `repr()` round-trips through
    `ast.literal_eval`, `{"status": "raised"}` when evaluating it raises,
    or `{"status": "not_literal"}` when the value has no literal form.
    `None` on any subprocess failure (timeout, crash, unreadable output) -
    the caller treats that the same as "no option".
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        output_path = Path(tmp_dir) / "result.json"
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _PROBE_SCRIPT,
                    str(worktree_path),
                    str(module_path),
                    expr_source,
                    str(output_path),
                ],
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except subprocess.SubprocessError:
            return None
        try:
            data = json.loads(output_path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or "status" not in data:
            return None
        return data


def _replace_raises_blocks(source: str, rewrites: list[tuple[ast.With, str, str]]) -> str:
    """Replace each `with pytest.raises(...):` block with a single
    `assert <expr> == <literal>` line at the block's own indentation,
    editing only those line ranges and leaving every other line of
    `source` byte-for-byte untouched."""
    lines = source.splitlines(keepends=True)
    for block, expr_source, literal_text in sorted(
        rewrites, key=lambda item: item[0].lineno, reverse=True
    ):
        start = block.lineno - 1
        end = block.end_lineno
        indent = " " * block.col_offset
        lines[start:end] = [f"{indent}assert {expr_source} == {literal_text}\n"]
    return "".join(lines)


def _function_has_assertion(node: ast.FunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.With) and _is_pytest_raises(child):
            return True
    return False


def _changed_functions_keep_assertions(source: str, function_names: set[str]) -> bool:
    """Final safety net (acceptance #5): every function this rewrite
    touched must still contain at least one assertion - never a bare,
    un-asserted call."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    found = {name: False for name in function_names}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in found:
            found[node.name] = _function_has_assertion(node)
    return all(found.values())


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


def _build_assertion_rewrite(
    worktree_path: Path, test_path: Path, source: str, test_names: set[str]
) -> str | None:
    """A deterministic rewrite of `source` that replaces each
    `pytest.raises(...)` block in `test_names`'s functions with
    `assert <call> == <value>`, built from the call's real current result
    in the shadow worktree - or None when no such rewrite can be made
    safely: no matching block, a block with more than one statement, a
    call that still raises, or a result with no literal form.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    blocks_by_function = _raises_blocks_by_function(tree, test_names)
    if not blocks_by_function:
        return None

    rewrites: list[tuple[ast.With, str, str]] = []
    for blocks in blocks_by_function.values():
        for block in blocks:
            expr_source = _single_statement_source(source, block)
            if expr_source is None:
                return None
            probe = _probe_expression(worktree_path, test_path, expr_source)
            if probe is None or probe.get("status") != "ok":
                return None
            literal_text = probe.get("repr")
            if not isinstance(literal_text, str):
                return None
            rewrites.append((block, expr_source, literal_text))

    rewritten = _replace_raises_blocks(source, rewrites)
    rewritten = _drop_bare_pytest_import(rewritten)
    if not _changed_functions_keep_assertions(rewritten, set(blocks_by_function)):
        return None
    return rewritten


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
            rewritten = _build_assertion_rewrite(shadow_path, test_path, original, test_names)
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
