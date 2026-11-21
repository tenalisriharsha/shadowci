"""Issue #71: a model's proposed fix may only change non-test files.

Ghost-patch verification only asks "does the full suite pass after the
fix?" — a fix that deletes or weakens the very test that would catch it,
skips it, or disables its collection via `conftest.py`/`pytest.ini`/etc.
passes that check by construction. `test_files_touched_by_diff` finds
every test file (by issue #71's definition below) a unified diff adds,
modifies, deletes, or renames, so the pipeline can refuse the fix outright
instead of ever verifying it.

A "test file" is: anything under the configured tests directory, anything
pytest would collect by name (`test_*.py`, `*_test.py`) wherever it lives,
every `conftest.py`, and `pytest.ini`/`tox.ini` (dedicated test-runner
config) or `setup.cfg`/`pyproject.toml` when the diff touches a
`[tool:pytest]`/`[tool.pytest` section — both of those also hold unrelated
project config, so only a change to their pytest section counts.
"""

from __future__ import annotations

from pathlib import Path

_ALWAYS_TEST_CONFIG_BASENAMES = frozenset({"pytest.ini", "tox.ini"})
_SECTION_GATED_BASENAMES = frozenset({"setup.cfg", "pyproject.toml"})
_PYTEST_SECTION_MARKERS = ("[tool.pytest", "[tool:pytest")


def _strip_diff_prefix(raw_path: str) -> str | None:
    path = raw_path.strip()
    if path in ("", "/dev/null"):
        return None
    return path.removeprefix("a/").removeprefix("b/")


def _iter_diff_file_sections(diff_text: str):
    """Yield `(old_path, new_path, body_lines)` for each file a unified
    diff touches, matching on the `--- `/`+++ ` header pair every file
    section has — whether or not it's preceded by a `diff --git` header —
    plus a pure `rename from`/`rename to` pair with no content hunk."""
    lines = diff_text.splitlines()
    i = 0
    n = len(lines)
    pending_rename_from: str | None = None
    while i < n:
        line = lines[i]
        if line.startswith("rename from "):
            pending_rename_from = line[len("rename from ") :].strip()
            i += 1
            continue
        if line.startswith("rename to ") and pending_rename_from is not None:
            yield pending_rename_from, line[len("rename to ") :].strip(), []
            pending_rename_from = None
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old_raw = line[len("--- ") :]
            new_raw = lines[i + 1][len("+++ ") :]
            i += 2
            body: list[str] = []
            while i < n and not lines[i].startswith("--- "):
                body.append(lines[i])
                i += 1
            yield old_raw, new_raw, body
            continue
        i += 1


def _is_pytest_config_section_touched(body: list[str]) -> bool:
    changed_lines = (line for line in body if line.startswith(("+", "-")))
    return any(marker in line for line in changed_lines for marker in _PYTEST_SECTION_MARKERS)


def test_files_touched_by_diff(diff_text: str, tests_dir: str = "tests") -> set[str]:
    """Every test file (by issue #71's definition) `diff_text` adds,
    modifies, deletes, or renames — added or removed, not just the
    post-patch path, so deleting or renaming a test file counts too."""
    tests_prefix = f"{tests_dir.rstrip('/')}/"
    touched: set[str] = set()
    for old_raw, new_raw, body in _iter_diff_file_sections(diff_text):
        candidates = {_strip_diff_prefix(old_raw), _strip_diff_prefix(new_raw)}
        for path in (candidate for candidate in candidates if candidate is not None):
            basename = Path(path).name
            if (
                path == tests_dir
                or path.startswith(tests_prefix)
                or (basename.startswith("test_") and basename.endswith(".py"))
                or basename.endswith("_test.py")
                or basename == "conftest.py"
                or basename in _ALWAYS_TEST_CONFIG_BASENAMES
                or (basename in _SECTION_GATED_BASENAMES and _is_pytest_config_section_touched(body))
            ):
                touched.add(path)
    return touched
