"""`shadowci doctor`: diagnose whether ShadowCI can work in this project.

Each check reports `OK`, `WARN`, or `FAIL` and, for anything not `OK`, a
`fix_hint` naming the exact command that fixes it. `--fix`
(`apply_fixes`) applies only the fixes that are always safe to make
automatically — recreating the shadow worktree, rebaselining a stale one
(issue #53), discarding stale ghost patches — and never touches a project
source file.
"""

from __future__ import annotations

import importlib.util
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from shadowci import pause as pause_module
from shadowci import pipeline
from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.head_check import head_sha
from shadowci.providers import (
    ClaudeCLIProvider,
    ClaudeCLIProviderError,
    create_provider,
)
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

OPTIONAL_TOOLS = ("pytest", "mypy", "ruff")


@dataclass(frozen=True)
class DoctorCheck:
    """One diagnostic result: a name, a status, and why."""

    name: str
    status: str
    message: str
    fix_hint: str | None = None


def _check_git(root: Path) -> DoctorCheck:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return DoctorCheck(
            "git repository", FAIL, "git is not installed", "Try: install git."
        )

    if result.returncode != 0 or result.stdout.strip() != "true":
        return DoctorCheck(
            "git repository",
            FAIL,
            f"{root} is not a git repository",
            f"Try: run `git init` in {root}.",
        )
    return DoctorCheck("git repository", OK, "this is a git repository")


def _load_config_for_doctor(root: Path) -> tuple[ShadowCIConfig, DoctorCheck]:
    config_path = root / DEFAULT_CONFIG_FILENAME
    if not config_path.exists():
        return ShadowCIConfig(project_root=root), DoctorCheck(
            "config", OK, f"no {DEFAULT_CONFIG_FILENAME} found, using defaults"
        )

    try:
        config = load_config(root)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a real FAIL
        return ShadowCIConfig(project_root=root), DoctorCheck(
            "config",
            FAIL,
            f"{config_path} could not be read: {exc}",
            f"Try: fix the syntax in {config_path}.",
        )
    return config, DoctorCheck("config", OK, f"{config_path} read successfully")


def _check_worktree(worktree: ShadowWorktree) -> DoctorCheck:
    if worktree.exists():
        return DoctorCheck(
            "shadow worktree", OK, f"shadow worktree ready at {worktree.path}"
        )
    return DoctorCheck(
        "shadow worktree",
        FAIL,
        f"no shadow worktree at {worktree.path}",
        "Try: run `shadowci init` (or `shadowci doctor --fix`).",
    )


def _check_shadow_head(root: Path, worktree: ShadowWorktree) -> DoctorCheck:
    """Issue #53: the shadow's `HEAD` never moved on its own — a commit,
    pull, checkout or rebase on the live tree leaves it stale, and every
    check run against it would then mix old and new files."""
    if not worktree.exists():
        return DoctorCheck("shadow head", OK, "no shadow worktree yet")

    live = head_sha(root)
    shadow = head_sha(worktree.path)
    if live == shadow:
        return DoctorCheck("shadow head", OK, "shadow worktree HEAD matches live HEAD")

    return DoctorCheck(
        "shadow head",
        WARN,
        f"shadow worktree HEAD ({shadow[:10]}) has drifted from live HEAD ({live[:10]})",
        "Try: run `shadowci doctor --fix` (or let `shadowci watch`/`radar` rebaseline it).",
    )


def _check_provider(config: ShadowCIConfig) -> DoctorCheck:
    try:
        provider = create_provider(config.provider)
    except ValueError as exc:
        return DoctorCheck(
            "provider",
            FAIL,
            str(exc),
            f"Try: fix `provider` in {DEFAULT_CONFIG_FILENAME}.",
        )

    if isinstance(provider, ClaudeCLIProvider):
        try:
            provider.health_check()
        except ClaudeCLIProviderError as exc:
            return DoctorCheck("provider", FAIL, str(exc))
        return DoctorCheck("provider", OK, "claude-cli answered a trivial prompt")

    return DoctorCheck(
        "provider", OK, f"provider '{config.provider}' needs no connectivity check"
    )


def _check_tool(tool: str) -> DoctorCheck:
    if importlib.util.find_spec(tool) is not None:
        return DoctorCheck(tool, OK, f"{tool} is importable")
    return DoctorCheck(
        tool, WARN, f"{tool} is not installed", f"Try: pip install {tool}."
    )


def _check_ghost_patches(root: Path) -> DoctorCheck:
    listings = pipeline.list_ghost_patches(root)
    if not listings:
        return DoctorCheck("ghost patches", OK, "no saved ghost patches")

    stale = [listing for listing in listings if not listing.ready]
    if not stale:
        return DoctorCheck(
            "ghost patches",
            OK,
            f"{len(listings)} saved ghost patch(es), all ready to apply",
        )

    names = ", ".join(listing.patch_path.stem for listing in stale)
    return DoctorCheck(
        "ghost patches",
        WARN,
        f"{len(stale)} stale ghost patch(es) no longer apply to the live tree: {names}",
        "Try: run `shadowci doctor --fix` (or `shadowci ghosts --clean`).",
    )


def _check_pause(root: Path) -> DoctorCheck:
    state = pause_module.current_pause(root)
    if state is None:
        return DoctorCheck("pause", OK, "not paused")

    if state.until is None:
        return DoctorCheck(
            "pause",
            WARN,
            "paused indefinitely — no fix will be generated",
            "Try: run `shadowci resume` when you're done.",
        )

    remaining = max(0.0, state.until - time.time())
    return DoctorCheck(
        "pause",
        WARN,
        f"paused, {remaining:.0f}s remaining — no fix will be generated",
        "Try: run `shadowci resume` to end it early.",
    )


def _check_wip_marker(root: Path) -> DoctorCheck:
    try:
        result = subprocess.run(
            ["git", "grep", "--fixed-strings", "--files-with-matches", pause_module.WIP_MARKER],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return DoctorCheck("wip marker", OK, "git is not installed, skipped")

    if result.returncode not in (0, 1):
        return DoctorCheck("wip marker", OK, "could not check git-tracked files for the wip marker")

    files = [line for line in result.stdout.splitlines() if line.strip()]
    if not files:
        return DoctorCheck("wip marker", OK, "no git-tracked file contains the wip marker")

    names = ", ".join(files)
    return DoctorCheck(
        "wip marker",
        WARN,
        f"{len(files)} git-tracked file(s) contain '{pause_module.WIP_MARKER}': {names}",
        "Try: remove the marker before committing — it was likely left in by accident.",
    )


def run_checks(root: Path) -> tuple[DoctorCheck, ...]:
    """Every diagnostic check for the project at `root`, in a fixed order."""
    config, config_check = _load_config_for_doctor(root)
    worktree = ShadowWorktree(config)

    checks = [
        _check_git(root),
        config_check,
        _check_worktree(worktree),
        _check_shadow_head(root, worktree),
        _check_provider(config),
    ]
    checks.extend(_check_tool(tool) for tool in OPTIONAL_TOOLS)
    checks.append(_check_ghost_patches(root))
    checks.append(_check_pause(root))
    checks.append(_check_wip_marker(root))
    return tuple(checks)


def apply_fixes(root: Path) -> list[str]:
    """Apply only the fixes that are always safe: recreate a missing shadow
    worktree, rebaseline one whose `HEAD` has drifted from the live tree's
    (issue #53), discard ghost patches that no longer apply. Never touches
    a project source file."""
    config, _ = _load_config_for_doctor(root)
    worktree = ShadowWorktree(config)
    applied: list[str] = []

    if not worktree.exists():
        try:
            worktree.ensure_created()
            applied.append(f"created shadow worktree at {worktree.path}")
        except ShadowWorktreeError as exc:
            applied.append(f"could not create shadow worktree: {exc}")

    if worktree.exists() and head_sha(root) != head_sha(worktree.path):
        try:
            worktree.rebaseline()
            applied.append(f"rebaselined shadow worktree to {head_sha(worktree.path)[:10]}")
        except ShadowWorktreeError as exc:
            applied.append(f"could not rebaseline shadow worktree: {exc}")

    for name in pipeline.clean_ghost_patches(root):
        applied.append(f"removed stale ghost patch: {name}")

    return applied
