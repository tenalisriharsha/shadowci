"""The shadow worktree: a real `git worktree` that mirrors your live edits.

ShadowCI never writes to your live working tree. Instead it keeps a second,
isolated checkout of the same repository — the shadow worktree — and copies
the raw bytes of changed files into it. All deterministic checks and LLM-driven
fixes (phases 2-3) happen there; only a verified `GhostPatch` ever travels
back to your live files, and only when you explicitly apply it.

Issue #53: the shadow is created once, at whatever commit was live when
`shadowci init` ran, and nothing ever moved it afterwards — a commit, pull,
branch switch or rebase on the live tree left the shadow's `HEAD` and files
stale. `rebaseline()` is the fix: it moves the shadow to the live tree's
current `HEAD` and overlays every uncommitted/untracked/deleted path, so the
shadow is always an exact mirror whenever checks are about to run.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, FileChangeEvent

SHADOW_BRANCH = "shadowci-shadow"


class ShadowWorktreeError(RuntimeError):
    """Raised when the underlying `git worktree` operation fails."""


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ShadowWorktreeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result


def _run_git_ok(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Like `_run_git`, but never raises — for best-effort/read-only calls."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


class ShadowWorktree:
    """Manages a dedicated git worktree that mirrors the live project tree."""

    def __init__(self, config: ShadowCIConfig) -> None:
        self._config = config
        self._root = config.project_root
        self._shadow_path = config.shadow_path

    @property
    def path(self) -> Path:
        return self._shadow_path

    def exists(self) -> bool:
        return self._shadow_path.is_dir() and (self._shadow_path / ".git").exists()

    def ensure_created(self) -> Path:
        """Create the shadow worktree if it doesn't already exist.

        Detached at the live tree's current `HEAD` — not on a branch. Issue
        #53: a worktree checked out onto a branch is never moved forward by
        anything other than this code, which made it trivial for the branch
        and the live tree to drift apart silently.
        """
        if self.exists():
            return self._shadow_path

        self._shadow_path.parent.mkdir(parents=True, exist_ok=True)
        # Clears any stale registration left behind if the shadow directory
        # was deleted out from under a previous worktree (acceptance test 8).
        _run_git_ok(["worktree", "prune"], cwd=self._root)
        sha = _run_git(["rev-parse", "HEAD"], cwd=self._root).stdout.strip()
        _run_git(
            ["worktree", "add", "--detach", str(self._shadow_path), sha],
            cwd=self._root,
        )
        return self._shadow_path

    def sync(self, events: list[FileChangeEvent]) -> list[Path]:
        """Mirror the live bytes behind `events` into the shadow worktree.

        Returns the shadow-relative paths that were written or removed.
        """
        if not self.exists():
            self.ensure_created()

        touched: list[Path] = []
        for event in events:
            relative = self._relative_to_root(event.path)
            shadow_target = self._shadow_path / relative

            if event.kind is ChangeKind.DELETED:
                if shadow_target.exists():
                    shadow_target.unlink()
                touched.append(relative)
                continue

            if not event.path.exists():
                continue  # already deleted again by the time we got here

            shadow_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(event.path, shadow_target)
            touched.append(relative)

        return touched

    def rebaseline(self) -> None:
        """Bring the shadow worktree back in line with the live tree
        (issue #53): the same `HEAD` commit, and the same working files for
        every path outside `ignore_dirs`.

        Only ever touches the configured shadow path — `_check_safe_target`
        raises `ShadowWorktreeError` rather than risk running `checkout
        --force`/`clean` anywhere else, including the live tree itself.
        """
        self._check_safe_target()

        if not self.exists():
            self.ensure_created()

        live_sha = _run_git(["rev-parse", "HEAD"], cwd=self._root).stdout.strip()
        _run_git(["checkout", "--detach", "--force", live_sha], cwd=self._shadow_path)
        _run_git(["clean", "-fd"], cwd=self._shadow_path)
        self._drop_legacy_branch_if_unused()
        self._overlay_live_uncommitted_state()

    def _check_safe_target(self) -> None:
        if self._same_path(self._shadow_path, self._root):
            raise ShadowWorktreeError(
                f"refusing to rebaseline: shadow path {self._shadow_path} "
                "is the project root"
            )

        if not self.exists():
            return  # about to be created fresh; nothing registered to check yet

        shadow_resolved = self._shadow_path.resolve()
        if shadow_resolved not in self._registered_worktree_paths():
            raise ShadowWorktreeError(
                f"refusing to rebaseline {self._shadow_path}: it is not a "
                f"registered git worktree of {self._root}"
            )

    @staticmethod
    def _same_path(a: Path, b: Path) -> bool:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return a == b

    def _registered_worktree_paths(self) -> set[Path]:
        result = _run_git_ok(["worktree", "list", "--porcelain"], cwd=self._root)
        if result.returncode != 0:
            return set()

        paths: set[Path] = set()
        for line in result.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            raw = line[len("worktree ") :]
            try:
                paths.add(Path(raw).resolve())
            except OSError:
                paths.add(Path(raw))
        return paths

    def _drop_legacy_branch_if_unused(self) -> None:
        """Remove the pre-#53 `shadowci-shadow` branch once nothing has it
        checked out. Migrating a legacy branch-based worktree is just the
        `checkout --detach --force` above — this only cleans up the branch
        ref it leaves behind."""
        branches = _run_git_ok(["branch", "--list", SHADOW_BRANCH], cwd=self._root)
        if not branches.stdout.strip():
            return
        _run_git_ok(["branch", "-D", SHADOW_BRANCH], cwd=self._root)

    def _overlay_live_uncommitted_state(self) -> None:
        """Copy every uncommitted/untracked path from the live tree into the
        shadow, and remove what the live tree no longer has — everything
        `git ls-files` reports outside `ignore_dirs`, not just
        `watch_extensions`."""
        result = _run_git_ok(
            [
                "ls-files",
                "-z",
                "--modified",
                "--others",
                "--deleted",
                "--exclude-standard",
            ],
            cwd=self._root,
        )
        if result.returncode != 0:
            return

        for relative in result.stdout.split("\0"):
            if not relative:
                continue
            relative_path = Path(relative)
            if self._is_ignored(relative_path):
                continue

            live_path = self._root / relative_path
            shadow_target = self._shadow_path / relative_path

            if live_path.is_file():
                shadow_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(live_path, shadow_target)
            elif shadow_target.exists():
                shadow_target.unlink()

    def _is_ignored(self, relative_path: Path) -> bool:
        ignore_dirs = self._config.ignore_dirs
        return bool(ignore_dirs) and any(part in ignore_dirs for part in relative_path.parts)

    def _relative_to_root(self, path: Path) -> Path:
        try:
            return path.relative_to(self._root)
        except ValueError:
            raise ShadowWorktreeError(
                f"refusing to sync path outside project root: {path}"
            ) from None

    def remove(self) -> None:
        """Tear down the shadow worktree (used by tests / `shadowci clean`)."""
        if not self._shadow_path.exists():
            return
        try:
            _run_git(
                ["worktree", "remove", "--force", str(self._shadow_path)],
                cwd=self._root,
            )
        except ShadowWorktreeError:
            shutil.rmtree(self._shadow_path, ignore_errors=True)
