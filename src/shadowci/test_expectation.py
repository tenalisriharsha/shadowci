"""Deterministically rewrite a failing test's expectation to match a user's
intentional behaviour change (issue #37's "update the test" option).

No model judgement is involved: the only test shape this rewrites is a
`with pytest.raises(...):` block whose body no longer raises. The block's
own expression is executed for real, in a fresh subprocess against whatever
code is currently sitting in the worktree it's pointed at, and the actual
result becomes the new assertion. Any other shape — or an expression that
still doesn't run cleanly — is refused rather than guessed at, so the
caller can fall back to treating the fix as an ordinary verified patch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path


class TestExpectationError(RuntimeError):
    """Raised when a failing test's expectation can't be rewritten deterministically."""


def _is_raises_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "raises"
    if isinstance(func, ast.Name):
        return func.id == "raises"
    return False


def _find_raises_block(tree: ast.Module) -> ast.With | None:
    """The first `with pytest.raises(...): <single expression>` block, if any."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if not any(_is_raises_call(item.context_expr) for item in node.items):
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Expr):
            return node
    return None


def _import_lines(tree: ast.Module, source: str) -> list[str]:
    """Every top-level import line except `import pytest`, which the
    rewritten test no longer needs — keeping it would leave an unused
    import behind for a linter to flag."""
    lines = []
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        segment = ast.get_source_segment(source, node)
        if segment is None:
            continue
        if isinstance(node, ast.Import) and any(alias.name == "pytest" for alias in node.names):
            continue
        lines.append(segment)
    return lines


def _pytest_still_referenced(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Name) and node.id == "pytest":
            return True
    return False


def build_updated_test_source(test_source: str, worktree_path: Path) -> str:
    """Rewrite `test_source`'s failing `pytest.raises` expectation to match
    the real, current behaviour of the code in `worktree_path`.

    Raises `TestExpectationError` when the test isn't this exact shape, or
    when the block's own expression still doesn't execute cleanly — the
    caller should treat that as "can't be rewritten", not as a crash.
    """
    try:
        tree = ast.parse(test_source)
    except SyntaxError as exc:
        raise TestExpectationError(f"could not parse test source: {exc}") from exc

    with_node = _find_raises_block(tree)
    if with_node is None:
        raise TestExpectationError("no rewritable pytest.raises expectation found")

    expr_statement = with_node.body[0]
    assert isinstance(expr_statement, ast.Expr)
    expr_source = ast.get_source_segment(test_source, expr_statement.value)
    if not expr_source:
        raise TestExpectationError("could not recover the raises block's expression source")

    import_lines = _import_lines(tree, test_source)
    probe = "\n".join(import_lines) + f"\nprint(repr({expr_source}))\n"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise TestExpectationError(
            f"the raises block's expression still doesn't run cleanly: {result.stderr.strip()}"
        )
    actual_repr = result.stdout.strip()

    lines = test_source.splitlines()
    indent = " " * with_node.col_offset
    new_line = f"{indent}assert {expr_source} == {actual_repr}"
    start = with_node.lineno - 1
    end = with_node.end_lineno if with_node.end_lineno is not None else with_node.lineno
    rewritten_lines = lines[:start] + [new_line] + lines[end:]

    if not _pytest_still_referenced(ast.parse("\n".join(rewritten_lines))):
        rewritten_lines = [
            line for line in rewritten_lines if line.strip() != "import pytest"
        ]

    rewritten = "\n".join(rewritten_lines).lstrip("\n")
    if test_source.endswith("\n") and not rewritten.endswith("\n"):
        rewritten += "\n"
    return rewritten
