"""Issue #38: did a test already fail at HEAD, before the user's own edit?

Classifying a failure needs to know whether the test that just failed was
already broken *before* this change set landed — if it was, the failure
isn't a regression of this edit at all. The shadow worktree mirrors the
user's live edits, so it can't answer that; this module spins up a second,
throwaway `git worktree` checked out detached at HEAD, runs the specific
pytest node ids there, and reports which of them passed.

Spinning up a worktree and running pytest is real work, so the result is
cached per commit sha under `.shadowci/` — the same HEAD is checked against
over and over as a user keeps editing (or as the evaluation harness replays
the same scenario), and nothing about a commit's own test results changes
once it's made.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HEAD_CACHE_FILENAME = ".shadowci/head_check_cache.json"
_FAILED_NODE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def head_sha(project_root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _load_cache(cache_path: Path) -> dict:
    if not cache_path.is_file():
        return {}
    raw = cache_path.read_text().strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))


def _run_node_ids_at_sha(project_root: Path, sha: str, node_ids: list[str]) -> dict[str, bool]:
    with tempfile.TemporaryDirectory(prefix="shadowci-head-") as tmp:
        checkout_path = Path(tmp) / "head"
        added = _run_git(["worktree", "add", "--detach", str(checkout_path), sha], project_root)
        if added.returncode != 0:
            # Can't verify this commit's own history — treat every node id
            # as "did not pass" rather than risk calling a real regression
            # pre-existing on no evidence.
            return {node_id: False for node_id in node_ids}

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *node_ids],
                cwd=checkout_path,
                capture_output=True,
                text=True,
                check=False,
            )
            output = result.stdout + result.stderr
            failed_ids = set(_FAILED_NODE_RE.findall(output))
            return {node_id: node_id not in failed_ids for node_id in node_ids}
        finally:
            _run_git(["worktree", "remove", "--force", str(checkout_path)], project_root)


def node_ids_passed_at_head(
    project_root: Path, node_ids: tuple[str, ...], cache_path: Path | None = None
) -> dict[str, bool]:
    """Whether each of `node_ids` (e.g. `"tests/test_x.py::test_x"`) passes
    against a detached checkout of `project_root`'s current HEAD, cached by
    commit sha so the same commit is never re-checked out twice."""
    if not node_ids:
        return {}

    sha = head_sha(project_root)
    resolved_cache_path = cache_path or (project_root / HEAD_CACHE_FILENAME)
    cache = _load_cache(resolved_cache_path)
    sha_cache: dict[str, bool] = dict(cache.get(sha, {}))

    missing = [node_id for node_id in node_ids if node_id not in sha_cache]
    if missing:
        sha_cache.update(_run_node_ids_at_sha(project_root, sha, missing))
        cache[sha] = sha_cache
        _save_cache(resolved_cache_path, cache)

    return {node_id: sha_cache[node_id] for node_id in node_ids}
