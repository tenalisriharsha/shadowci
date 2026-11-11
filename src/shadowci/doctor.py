"""Diagnose whether ShadowCI can work in this project, and how to fix it.

Each check in `run_checks` reports `OK`, `WARN`, or `FAIL` with a short
detail and — for anything not `OK` — a `Try:` hint naming the exact fix
command. `apply_safe_fixes` backs the `doctor --fix` flag: it only
recreates the shadow worktree and discards stale ghost patches, both
reversible operations on ShadowCI's own state, and never touches a
project source file.
"""

from __future__ import annotations

import importlib.util
import subprocess
from dataclasses import dataclass
from pathlib import Path

from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.pipeline import clean_ghost_patches, list_ghost_patches
from shadowci.providers import create_provider
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError

OPTIONAL_TOOLS = ("pytest", "mypy", "ruff")


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnostic line: a name, a status, the detail behind it, and —
    for anything not `OK` — a `Try:` hint naming the fix, or `None` when
    the detail already spells one out."""

    name: str
    status: str
    detail: str
    try_hint: str | None = None


def _check_git_repo(root: Path) -> DoctorCheck:
    name = "git repository"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, NotADirectoryError) as exc:
        return DoctorCheck(name, "FAIL", str(exc), "run `git init` inside the project, then `shadowci init`.")

    if result.returncode != 0 or result.stdout.strip() != "true":
        detail = result.stderr.strip() or f"{root} is not a git repository"
        return DoctorCheck(name, "FAIL", detail, "run `git init` inside the project, then `shadowci init`.")
    return DoctorCheck(name, "OK", str(root))


def _check_config(root: Path) -> tuple[DoctorCheck, ShadowCIConfig]:
    name = "shadowci.toml"
    config_path = root / DEFAULT_CONFIG_FILENAME
    try:
        config = load_config(root)
    except Exception as exc:  # noqa: BLE001 - reading a user-edited file is a boundary
        check = DoctorCheck(
            name,
            "FAIL",
            f"could not read {config_path}: {exc}",
            f"fix the TOML syntax in {config_path}, or delete it to use defaults.",
        )
        return check, ShadowCIConfig(project_root=root)

    if not config_path.exists():
        return DoctorCheck(name, "OK", "no shadowci.toml, using defaults"), config
    return DoctorCheck(name, "OK", str(config_path)), config


def _check_worktree(config: ShadowCIConfig) -> DoctorCheck:
    name = "shadow worktree"
    worktree = ShadowWorktree(config)
    if worktree.exists():
        return DoctorCheck(name, "OK", f"ready at {worktree.path}")
    return DoctorCheck(
        name,
        "FAIL",
        f"not created yet ({worktree.path})",
        "run `shadowci init`, or `shadowci doctor --fix`.",
    )


def _check_provider(config: ShadowCIConfig) -> DoctorCheck:
    name = f"provider ({config.provider})"
    try:
        provider = create_provider(config.provider)
    except ValueError as exc:
        return DoctorCheck(
            name, "FAIL", str(exc), f"set provider to a known provider in {DEFAULT_CONFIG_FILENAME}."
        )

    try:
        provider.ping()
    except Exception as exc:  # noqa: BLE001 - any provider call failure is a boundary
        detail = str(exc)
        try_hint = None if "Try:" in detail else "re-run the provider by hand to see the full error."
        return DoctorCheck(name, "FAIL", detail, try_hint)
    return DoctorCheck(name, "OK", "answered")


def _check_tool_importable(tool: str) -> DoctorCheck:
    if importlib.util.find_spec(tool) is not None:
        return DoctorCheck(tool, "OK", "importable")
    return DoctorCheck(tool, "WARN", f"{tool} not installed", f"pip install {tool}")


def _check_ghost_patches(root: Path) -> list[DoctorCheck]:
    checks = []
    for listing in list_ghost_patches(root):
        name = f"ghost patch {listing.digest}"
        if listing.ready:
            checks.append(DoctorCheck(name, "OK", "ready to apply"))
        else:
            checks.append(
                DoctorCheck(
                    name,
                    "FAIL",
                    "stale, no longer applies to the live tree",
                    "`shadowci doctor --fix`, or `shadowci ghosts --clean`.",
                )
            )
    return checks


def run_checks(root: Path) -> list[DoctorCheck]:
    """Every doctor check against the project at `root`, in a fixed order."""
    checks = [_check_git_repo(root)]

    config_check, config = _check_config(root)
    checks.append(config_check)
    checks.append(_check_worktree(config))
    checks.append(_check_provider(config))

    for tool in OPTIONAL_TOOLS:
        checks.append(_check_tool_importable(tool))

    checks.extend(_check_ghost_patches(root))
    return checks


def apply_safe_fixes(root: Path) -> None:
    """Repair what `doctor --fix` is allowed to touch: ShadowCI's own state,
    never a project source file."""
    try:
        config = load_config(root)
    except Exception:  # noqa: BLE001 - an unreadable config just skips the worktree fix
        config = None

    if config is not None:
        worktree = ShadowWorktree(config)
        if not worktree.exists():
            try:
                worktree.ensure_created()
            except ShadowWorktreeError:
                pass

    clean_ghost_patches(root)
