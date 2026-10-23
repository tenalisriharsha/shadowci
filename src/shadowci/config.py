"""Configuration for a ShadowCI run.

Loaded from a ``shadowci.toml`` at the project root when present, otherwise
every field falls back to a default tuned for interactive use.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11
    import tomli as tomllib

DEFAULT_CONFIG_FILENAME = "shadowci.toml"
DEFAULT_SHADOW_DIR = ".shadowci/shadow"
DEFAULT_WATCH_EXTENSIONS = (".py",)
DEFAULT_IGNORE_DIRS = (
    ".git",
    ".shadowci",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
)


@dataclass
class ShadowCIConfig:
    """Tunable knobs for watching, debouncing, and the shadow worktree."""

    project_root: Path = field(default_factory=Path.cwd)
    shadow_dir: Path = field(default_factory=lambda: Path(DEFAULT_SHADOW_DIR))
    watch_extensions: tuple[str, ...] = DEFAULT_WATCH_EXTENSIONS
    ignore_dirs: tuple[str, ...] = DEFAULT_IGNORE_DIRS
    debounce_seconds: float = 0.75
    idle_seconds: float = 2.0
    poll_interval_seconds: float = 0.25
    provider: str = "scripted"

    @property
    def shadow_path(self) -> Path:
        path = self.shadow_dir
        if path.is_absolute():
            return path
        return self.project_root / path

    def should_watch(self, path: Path) -> bool:
        """Whether `path` is within scope: right extension, not ignored."""
        if self.ignore_dirs and any(part in self.ignore_dirs for part in path.parts):
            return False
        if not self.watch_extensions:
            return True
        return path.suffix in self.watch_extensions


def load_config(project_root: Path | None = None) -> ShadowCIConfig:
    """Load config from ``<project_root>/shadowci.toml``, or defaults if absent."""
    root = Path(project_root) if project_root is not None else Path.cwd()
    config_path = root / DEFAULT_CONFIG_FILENAME

    if not config_path.exists():
        return ShadowCIConfig(project_root=root)

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    section = raw.get("shadowci", raw)
    kwargs: dict = {"project_root": root}

    if "shadow_dir" in section:
        kwargs["shadow_dir"] = Path(section["shadow_dir"])
    if "watch_extensions" in section:
        kwargs["watch_extensions"] = tuple(section["watch_extensions"])
    if "ignore_dirs" in section:
        kwargs["ignore_dirs"] = tuple(section["ignore_dirs"])
    if "debounce_seconds" in section:
        kwargs["debounce_seconds"] = float(section["debounce_seconds"])
    if "idle_seconds" in section:
        kwargs["idle_seconds"] = float(section["idle_seconds"])
    if "poll_interval_seconds" in section:
        kwargs["poll_interval_seconds"] = float(section["poll_interval_seconds"])
    if "provider" in section:
        kwargs["provider"] = str(section["provider"])

    return ShadowCIConfig(**kwargs)
