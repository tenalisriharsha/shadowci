"""The `shadowci` command line interface.

Phase 1 wires together the watcher, change-set detector, and shadow
worktree so the foundation is observable end to end. Deterministic checks,
LLM escalation, ghost patches, the interruption policy, and the bug-radar
TUI arrive in later phases.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from shadowci.changeset import ChangeSetDetector
from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError
from shadowci.watcher import PollingFileWatcher


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config_path = root / DEFAULT_CONFIG_FILENAME

    if not config_path.exists():
        config_path.write_text(
            "[shadowci]\n"
            'shadow_dir = ".shadowci/shadow"\n'
            "debounce_seconds = 0.75\n"
            "idle_seconds = 2.0\n"
            'provider = "scripted"\n'
        )
        print(f"wrote {config_path}")
    else:
        print(f"{config_path} already exists, leaving it alone")

    config = load_config(root)
    try:
        worktree = ShadowWorktree(config)
        shadow_path = worktree.ensure_created()
    except ShadowWorktreeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"shadow worktree ready at {shadow_path}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config = load_config(root)
    worktree = ShadowWorktree(config)

    print(f"project root     : {config.project_root}")
    print(f"shadow worktree  : {config.shadow_path} ({'exists' if worktree.exists() else 'not created'})")
    print(f"watch extensions : {', '.join(config.watch_extensions) or '(all)'}")
    print(f"debounce / idle  : {config.debounce_seconds}s / {config.idle_seconds}s")
    print(f"provider         : {config.provider}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config = load_config(root)
    if args.interval is not None:
        config.poll_interval_seconds = args.interval

    watcher = PollingFileWatcher(config)
    detector = ChangeSetDetector(config)
    worktree = ShadowWorktree(config)

    print(f"watching {config.project_root} (poll every {config.poll_interval_seconds}s)")

    cycle = 0
    while args.max_cycles is None or cycle < args.max_cycles:
        events = watcher.poll()
        if events:
            detector.add_events(events)
            for event in events:
                print(f"  [{event.kind.value}] {event.path.relative_to(root)}")

        changeset = detector.poll()
        if changeset is not None:
            touched = worktree.sync(list(changeset.events))
            print(
                f"changeset ready: {len(changeset.paths)} file(s) -> "
                f"synced {len(touched)} into shadow worktree"
            )

        cycle += 1
        if args.max_cycles is None or cycle < args.max_cycles:
            time.sleep(config.poll_interval_seconds)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shadowci",
        description=(
            "An ambient coding agent that watches your code and prepares "
            "verified bug fixes without touching your working tree."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="set up config + shadow worktree")
    init_parser.add_argument("path", nargs="?", default=".")
    init_parser.set_defaults(func=_cmd_init)

    status_parser = subparsers.add_parser("status", help="show current configuration")
    status_parser.add_argument("path", nargs="?", default=".")
    status_parser.set_defaults(func=_cmd_status)

    watch_parser = subparsers.add_parser("watch", help="watch for changes and sync the shadow worktree")
    watch_parser.add_argument("path", nargs="?", default=".")
    watch_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="stop after this many poll cycles (default: run forever)",
    )
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="override the poll interval in seconds",
    )
    watch_parser.set_defaults(func=_cmd_watch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
