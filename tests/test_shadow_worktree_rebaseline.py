"""Regression tests for issue #53: the shadow worktree goes stale.

`ShadowWorktree` is created once, at whatever commit was live when
`shadowci init` ran, on a branch called `shadowci-shadow`
(`shadow_worktree.py::ensure_created`). Nothing ever moves it afterwards:
`sync()` only ever copies the exact paths behind a `FileChangeEvent`, so a
commit, pull, branch switch or rebase on the live tree is never reflected
in the shadow's `HEAD` or in files that weren't individually re-synced.

The invariant the fix must establish: whenever checks are about to run,
the shadow has the same `HEAD` commit as the live tree, and the same
working files for every path outside `ignore_dirs`. That is the job of one
new routine, `ShadowWorktree.rebaseline()`:

- inside the shadow only: `git checkout --detach --force <live HEAD sha>`
  then `git clean -fd`;
- overlay every path `git ls-files -z --modified --others --deleted
  --exclude-standard` reports in the *live* tree (skipping `ignore_dirs`):
  copy live bytes in, delete what the live tree no longer has.
- refuse (raise `ShadowWorktreeError`) rather than touch anything unless
  the target is the configured shadow path, isn't the project root, and
  is a registered worktree of this repository.

New worktrees are created detached (`git worktree add --detach`), not on
`shadowci-shadow` — `rebaseline()` must also migrate an existing worktree
still parked on that branch and drop the now-unused branch.

These tests exercise `rebaseline()` and the now-detached `ensure_created()`
directly; they must fail against the unfixed code (`rebaseline` does not
exist yet, and `ensure_created()` still checks the shadow out onto
`shadowci-shadow`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shadowci.config import ShadowCIConfig
from shadowci.shadow_worktree import SHADOW_BRANCH, ShadowWorktree, ShadowWorktreeError


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def _git_ok(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _head_sha(cwd: Path) -> str:
    return _run_git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _diff_head(cwd: Path) -> str:
    return _run_git(["diff", "HEAD"], cwd=cwd).stdout


# ---------------------------------------------------------------------------
# New worktrees are detached, not on `shadowci-shadow` (part of the fix: see
# "Detached, no branch" in the issue).
# ---------------------------------------------------------------------------


def test_ensure_created_checks_out_detached_not_the_shadow_branch(git_repo: Path):
    worktree = ShadowWorktree(_config(git_repo))
    worktree.ensure_created()

    ref = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree.path
    ).stdout.strip()
    assert ref == "HEAD", f"expected a detached HEAD, got branch {ref!r}"

    branches = _run_git(["branch", "--list", SHADOW_BRANCH], cwd=git_repo).stdout
    assert branches.strip() == "", "ensure_created() must not leave a shadowci-shadow branch"


# ---------------------------------------------------------------------------
# Safety: rebaseline() may only ever touch the configured shadow worktree.
# ---------------------------------------------------------------------------


def test_rebaseline_refuses_when_configured_shadow_path_is_the_project_root(
    git_repo: Path,
):
    config = ShadowCIConfig(project_root=git_repo, shadow_dir=Path("."))
    worktree = ShadowWorktree(config)

    with pytest.raises(ShadowWorktreeError):
        worktree.rebaseline()

    # and it must not have touched the live tree on the way to refusing
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_rebaseline_leaves_the_live_tree_byte_for_byte_and_git_status_unchanged(
    git_repo: Path,
):
    worktree = ShadowWorktree(_config(git_repo))
    worktree.ensure_created()

    _write(git_repo, "app.py", "def add(a, b):\n    return a + b  # tweak\n")
    before_status = _git_ok(["status", "--porcelain"], cwd=git_repo).stdout
    before_bytes = (git_repo / "app.py").read_bytes()

    worktree.rebaseline()

    after_status = _git_ok(["status", "--porcelain"], cwd=git_repo).stdout
    after_bytes = (git_repo / "app.py").read_bytes()
    assert after_status == before_status
    assert after_bytes == before_bytes


# ---------------------------------------------------------------------------
# Acceptance test 1: commit while watching.
# ---------------------------------------------------------------------------


def test_rebaseline_moves_shadow_head_to_match_live_after_a_commit(git_repo: Path):
    (git_repo / ".gitignore").write_text(".shadowci/\n")
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    live_file = git_repo / "app.py"
    live_file.write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n\n"
        "def median(values):\n"
        "    s = sorted(values)\n"
        "    n = len(s)\n"
        "    mid = n // 2\n"
        "    if n % 2:\n"
        "        return s[mid]\n"
        "    return (s[mid - 1] + s[mid]) / 2\n"
    )
    worktree.rebaseline()
    _commit_all(git_repo, "add median")

    live_file.write_text(live_file.read_text() + "\n# a trailing comment\n")

    worktree.rebaseline()

    assert _head_sha(worktree.path) == _head_sha(git_repo)
    assert _diff_head(worktree.path) == _diff_head(git_repo)
    assert "median" in (worktree.path / "app.py").read_text()


def test_rebaseline_overlays_the_uncommitted_edit_after_moving_head(git_repo: Path):
    (git_repo / ".gitignore").write_text(".shadowci/\n")
    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    live_file = git_repo / "app.py"
    live_file.write_text("def add(a, b):\n    return a + b + 1\n")
    _commit_all(git_repo, "tweak add")

    live_file.write_text("def add(a, b):\n    return a + b + 1  # uncommitted\n")

    worktree.rebaseline()

    assert (worktree.path / "app.py").read_text() == live_file.read_text()
    assert _head_sha(worktree.path) == _head_sha(git_repo)


# ---------------------------------------------------------------------------
# Acceptance test 3 / 8: an uncommitted edit made before the shadow exists
# (or while it's missing) is still mirrored once it's (re)created.
# ---------------------------------------------------------------------------


def test_rebaseline_mirrors_an_uncommitted_edit_that_predates_shadow_creation(
    git_repo: Path,
):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # pre-existing edit\n")

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()
    worktree.rebaseline()

    assert (worktree.path / "app.py").read_text() == (git_repo / "app.py").read_text()


def test_rebaseline_recreates_a_full_mirror_after_the_shadow_directory_is_deleted(
    git_repo: Path,
):
    import shutil

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()

    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b  # edit before deletion\n")
    worktree.rebaseline()
    assert (worktree.path / "app.py").read_text() == (git_repo / "app.py").read_text()

    shutil.rmtree(worktree.path)
    assert not worktree.exists()

    worktree.ensure_created()
    worktree.rebaseline()

    assert worktree.exists()
    assert (worktree.path / "app.py").read_text() == (git_repo / "app.py").read_text()


# ---------------------------------------------------------------------------
# Acceptance test 5: deleted and untracked files, ignore_dirs respected.
# ---------------------------------------------------------------------------


def test_rebaseline_removes_deleted_adds_untracked_and_skips_ignored_dirs(
    git_repo: Path,
):
    _write(git_repo, "keep.py", "VALUE = 1\n")
    _commit_all(git_repo, "add keep.py")

    config = _config(git_repo)
    worktree = ShadowWorktree(config)
    worktree.ensure_created()
    assert (worktree.path / "keep.py").exists()

    (git_repo / "keep.py").unlink()
    _write(git_repo, "brand_new.py", "NEW = 2\n")
    ignored_dir = git_repo / "__pycache__"
    ignored_dir.mkdir()
    (ignored_dir / "leftover.pyc").write_text("not source")

    worktree.rebaseline()

    assert not (worktree.path / "keep.py").exists()
    assert (worktree.path / "brand_new.py").read_text() == "NEW = 2\n"
    assert not (worktree.path / "__pycache__").exists()


# ---------------------------------------------------------------------------
# Acceptance test 7: migrating a worktree created by the unfixed code
# (still parked on the shadowci-shadow branch).
# ---------------------------------------------------------------------------


def test_rebaseline_migrates_a_legacy_branch_based_worktree_and_removes_the_branch(
    git_repo: Path,
):
    config = _config(git_repo)
    shadow_path = config.shadow_path
    shadow_path.parent.mkdir(parents=True, exist_ok=True)

    _run_git(["branch", SHADOW_BRANCH, "HEAD"], cwd=git_repo)
    _run_git(["worktree", "add", str(shadow_path), SHADOW_BRANCH], cwd=git_repo)

    branch_ref_before = _run_git(
        ["-C", str(shadow_path), "rev-parse", "--abbrev-ref", "HEAD"], cwd=git_repo
    ).stdout.strip()
    assert branch_ref_before == SHADOW_BRANCH

    worktree = ShadowWorktree(config)
    worktree.rebaseline()

    branch_ref_after = _run_git(
        ["rev-parse", "--abbrev-ref", "HEAD"], cwd=shadow_path
    ).stdout.strip()
    assert branch_ref_after == "HEAD", "shadow must be detached after migration"

    remaining = _run_git(["branch", "--list", SHADOW_BRANCH], cwd=git_repo).stdout
    assert remaining.strip() == "", "the shadowci-shadow branch must be removed once migrated"


# ---------------------------------------------------------------------------
# Safety guard: rebaseline() must never run checkout/clean outside the
# configured shadow path.
# ---------------------------------------------------------------------------


def test_rebaseline_refusing_the_project_root_never_touches_the_live_tree(
    git_repo: Path,
):
    original = (git_repo / "app.py").read_text()
    config = ShadowCIConfig(project_root=git_repo, shadow_dir=Path("."))
    worktree = ShadowWorktree(config)

    with pytest.raises(ShadowWorktreeError):
        worktree.rebaseline()

    assert (git_repo / "app.py").read_text() == original
    status = _git_ok(["status", "--porcelain"], cwd=git_repo).stdout
    assert status.strip() == ""


def test_rebaseline_on_a_path_that_is_not_a_registered_worktree_refuses(
    tmp_path: Path, git_repo: Path
):
    stray = tmp_path / "not-a-worktree"
    stray.mkdir()
    (stray / ".git").write_text("gitdir: /nowhere\n")

    config = _config(git_repo)
    config.shadow_dir = stray
    worktree = ShadowWorktree(config)

    with pytest.raises(ShadowWorktreeError):
        worktree.rebaseline()
