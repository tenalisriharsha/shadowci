"""Issue #52: the baseline the revert check and model prompts diff against.

Issue #53 made the shadow worktree's `HEAD` track the live tree's current
commit, but a long session with no commit in between still has only one
`HEAD` for every edit in it. `revert_check.diff_head`/`diff_head_reversed`
and `providers.prompts`'s own diff compared against that single `HEAD`, so
an earlier, already-green edit (no commit separating it from the one being
checked now) was folded into "the user's own edit" for every edit that
followed it.

The fix: once a change set's checks all pass, `advance_baseline` snapshots
the shadow worktree's current working tree as a plain git commit object —
parented on the worktree's real `HEAD`, but pointed to by nothing (no
branch, no ref) so it never affects `git status`, a real commit, or any
other git-check in this codebase — and remembers its sha in a file under
the project's `.shadowci/` directory. `current_baseline_ref` is what
`diff_head`/`diff_head_reversed`/the prompt diff use in place of the
literal `"HEAD"` from then on: that snapshot if one was saved and its
commit object still exists, `"HEAD"` otherwise (no snapshot yet, or an old
project that predates this file) — so "the user's own edit" always means
"since the last time everything was green", not "since the last commit".
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

BASELINE_FILENAME = ".shadowci/baseline_sha"


def baseline_path(root: Path) -> Path:
    return root / BASELINE_FILENAME


def read_baseline_sha(root: Path) -> str | None:
    """The saved baseline commit sha, or `None` when no snapshot has been
    taken yet (a fresh project, or one from before this fix)."""
    path = baseline_path(root)
    if not path.is_file():
        return None
    sha = path.read_text().strip()
    return sha or None


def _commit_exists(worktree_path: Path, sha: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def current_baseline_ref(worktree_path: Path, root: Path) -> str:
    """The git ref/sha `diff_head`/`diff_head_reversed`/the prompt diff
    should compare the shadow worktree against: the saved snapshot commit
    if one exists and is still a valid object, otherwise `"HEAD"`."""
    sha = read_baseline_sha(root)
    if sha is not None and _commit_exists(worktree_path, sha):
        return sha
    return "HEAD"


def advance_baseline(worktree_path: Path, root: Path) -> str | None:
    """Snapshot the shadow worktree's current working tree as a new
    baseline commit, and save its sha. Only ever called after a fully
    green check run — a failing run must never move the baseline.

    The snapshot is built through a throwaway index file (`GIT_INDEX_FILE`)
    so the shadow worktree's real index is never touched, then committed
    with `commit-tree` — which creates the commit object without checking
    it out or moving any ref, branch, or `HEAD`. Returns the new sha, or
    `None` (leaving any existing baseline exactly as it was) if the
    snapshot can't be built for any reason — a project without a usable
    git identity, for instance.
    """
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        with tempfile.TemporaryDirectory() as tmp_dir:
            index_file = str(Path(tmp_dir) / "index")
            env = {**os.environ, "GIT_INDEX_FILE": index_file}
            subprocess.run(
                ["git", "read-tree", head_sha],
                cwd=worktree_path,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=worktree_path,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            tree_sha = subprocess.run(
                ["git", "write-tree"],
                cwd=worktree_path,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()

        commit_sha = subprocess.run(
            ["git", "commit-tree", tree_sha, "-p", head_sha, "-m", "shadowci baseline snapshot"],
            cwd=worktree_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return None

    path = baseline_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(commit_sha + "\n")
    return commit_sha
