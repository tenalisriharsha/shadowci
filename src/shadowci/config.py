"""Configuration for a ShadowCI run.

Loaded from a ``shadowci.toml`` at the project root when present, otherwise
every field falls back to a default tuned for interactive use.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from shadowci.interruption import DEFAULT_MIN_SIGNALS, DEFAULT_THRESHOLD

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11
    import tomli as tomllib

DEFAULT_CONFIG_FILENAME = "shadowci.toml"
DEFAULT_SHADOW_DIR = ".shadowci/shadow"
# Issue #72: a hanging check (an infinite loop, a deadlock, a hung network
# wait in a covered test) must not block watch/radar forever — every check
# subprocess is killed past this many seconds.
DEFAULT_CHECK_TIMEOUT_SECONDS = 60.0
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
    interruption_threshold: float = DEFAULT_THRESHOLD
    interruption_min_signals: int = DEFAULT_MIN_SIGNALS
    check_timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS

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
    if "interruption_threshold" in section:
        kwargs["interruption_threshold"] = float(section["interruption_threshold"])
    if "interruption_min_signals" in section:
        kwargs["interruption_min_signals"] = int(section["interruption_min_signals"])
    if "check_timeout_seconds" in section:
        kwargs["check_timeout_seconds"] = float(section["check_timeout_seconds"])

    return ShadowCIConfig(**kwargs)
