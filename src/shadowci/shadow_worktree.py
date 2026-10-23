"""The shadow worktree: a real `git worktree` that mirrors your live edits.

ShadowCI never writes to your live working tree. Instead it keeps a second,
isolated checkout of the same repository — the shadow worktree — and copies
the raw bytes of changed files into it. All deterministic checks and LLM-driven
fixes (phases 2-3) happen there; only a verified `GhostPatch` ever travels
back to your live files, and only when you explicitly apply it.
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
        """Create the shadow worktree if it doesn't already exist."""
        if self.exists():
            return self._shadow_path

        self._shadow_path.parent.mkdir(parents=True, exist_ok=True)

        branches = _run_git(["branch", "--list", SHADOW_BRANCH], cwd=self._root)
        if not branches.stdout.strip():
            _run_git(["branch", SHADOW_BRANCH, "HEAD"], cwd=self._root)

        _run_git(
            ["worktree", "add", str(self._shadow_path), SHADOW_BRANCH],
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

    def _relative_to_root(self, path: Path) -> Path:
        try:
            return path.relative_to(self._root)
        except ValueError:
            return path

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
