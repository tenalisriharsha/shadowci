"""The `shadowci` command line interface.

Phase 1 wired together the watcher, change-set detector, and shadow
worktree so the foundation is observable end to end. Phase 2 added
deterministic checks (tests/type checker/linter) run in the shadow
worktree after every stable change set, and escalation to an `LLMProvider`
only when one of them actually fails. Phase 3 takes what the provider
writes and verifies it for real inside the shadow worktree, then saves a
ghost patch to disk that `apply-ghost` can apply to the live tree in one
command. Phase 4 adds the interruption policy: every verified patch is
still saved, but `watch` now also reports whether the policy would
surface it or stay silent, and `apply-ghost` / `reject-ghost` /
`ignore-ghost` record the accept/reject/ignore signal that trains it. The
bug-radar TUI arrives later.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from shadowci.changeset import ChangeSetDetector
from shadowci.checks.suite import build_check_runners, run_checks
from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.escalation import EscalationPolicy
from shadowci.events import ChangeSet, CheckResult, GhostPatch, Signal
from shadowci.ghost import GhostPatchBuilder, GhostPatchError, apply_to_live_tree
from shadowci.interruption import InterruptionPolicy, fingerprint_for_patch
from shadowci.providers import create_provider
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError
from shadowci.signals import SignalStore
from shadowci.watcher import PollingFileWatcher

GHOST_PATCH_DIRNAME = ".shadowci/ghost"
SIGNALS_FILENAME = ".shadowci/signals.json"


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
    print(
        f"interruption     : threshold={config.interruption_threshold}, "
        f"min signals={config.interruption_min_signals}"
    )
    return 0


def _print_check_result(result: CheckResult) -> None:
    status = "PASS" if result.passed else "FAIL"
    print(f"  check [{result.name}] {status} ({result.duration_seconds:.2f}s)")
    if not result.passed and result.output.strip():
        for line in result.output.strip().splitlines()[:10]:
            print(f"    {line}")


def _ghost_patch_dir(root: Path) -> Path:
    path = root / GHOST_PATCH_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _signal_store(root: Path) -> SignalStore:
    return SignalStore(root / SIGNALS_FILENAME)


def _metadata_path(patch_path: Path) -> Path:
    return patch_path.with_suffix(".json")


def _load_patch_metadata(patch_path: Path) -> dict | None:
    """The fingerprint/digest sidecar saved alongside a ghost patch, if any.

    Patch files handed to the CLI by hand (as in a scripted apply) won't
    have one — in that case the signal simply isn't recorded, rather than
    guessing a fingerprint.
    """
    metadata_path = _metadata_path(patch_path)
    if not metadata_path.is_file():
        return None
    return json.loads(metadata_path.read_text())


def _save_ghost_patch(patch: GhostPatch, root: Path) -> Path:
    digest = hashlib.sha1(patch.diff.encode("utf-8")).hexdigest()[:10]
    patch_path = _ghost_patch_dir(root) / f"{digest}.patch"
    patch_path.write_text(patch.diff)
    _metadata_path(patch_path).write_text(
        json.dumps(
            {
                "digest": digest,
                "fingerprint": fingerprint_for_patch(patch),
                "summary": patch.evidence.summary,
            },
            indent=2,
        )
    )
    return patch_path


def _run_checks_and_maybe_escalate(
    changeset: ChangeSet, worktree: ShadowWorktree, config: ShadowCIConfig
) -> None:
    runners = build_check_runners(changeset, worktree.path)
    results = run_checks(runners, worktree.path)
    for result in results:
        _print_check_result(result)

    failing = tuple(result for result in results if not result.passed)
    if not failing:
        return

    try:
        provider = create_provider(config.provider)
    except ValueError as exc:
        print(f"  skipping escalation: {exc}", file=sys.stderr)
        return

    print(f"  escalating to provider '{provider.name}' ({len(failing)} check(s) failed)")
    try:
        policy = EscalationPolicy(provider)
        escalation = policy.maybe_escalate(changeset, results)
    except RuntimeError as exc:
        print(f"  provider error: {exc}", file=sys.stderr)
        return

    if escalation is None:
        return

    print("  regression test:")
    for line in escalation.regression_test.splitlines():
        print(f"    {line}")
    print("  proposed fix:")
    for line in escalation.proposed_fix.splitlines():
        print(f"    {line}")

    print("  verifying fix in shadow worktree...")
    try:
        builder = GhostPatchBuilder(worktree)
        patch = builder.build(
            changeset,
            escalation.failing_checks,
            escalation.regression_test,
            escalation.proposed_fix,
        )
    except GhostPatchError as exc:
        print(f"  could not verify a ghost patch: {exc}", file=sys.stderr)
        return

    patch_path = _save_ghost_patch(patch, config.project_root)
    print(f"  ghost patch verified: {patch.evidence.summary}")
    print(f"  saved to {patch_path}")

    policy = InterruptionPolicy(
        _signal_store(config.project_root),
        threshold=config.interruption_threshold,
        min_signals=config.interruption_min_signals,
    )
    decision = policy.evaluate(patch)
    if decision.interrupt:
        print(
            f"  interruption policy: surfacing this patch "
            f"(score={decision.score:.2f} over {decision.sample_size} past signal(s))"
        )
    else:
        print(
            f"  interruption policy: staying silent for now "
            f"(score={decision.score:.2f} over {decision.sample_size} past signal(s)) "
            "— patch is saved, review it whenever you like"
        )
    print(f"  apply with: shadowci apply-ghost {patch_path}")


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
            if not args.skip_checks:
                _run_checks_and_maybe_escalate(changeset, worktree, config)

        cycle += 1
        if args.max_cycles is None or cycle < args.max_cycles:
            time.sleep(config.poll_interval_seconds)

    return 0


def _cmd_apply_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    if not patch_file.is_file():
        print(f"error: no such patch file: {patch_file}", file=sys.stderr)
        return 1

    diff = patch_file.read_text()
    try:
        apply_to_live_tree(diff, root)
    except GhostPatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"applied {patch_file} to {root}")

    metadata = _load_patch_metadata(patch_file)
    if metadata is not None:
        _signal_store(root).record(metadata["fingerprint"], Signal.ACCEPT, metadata["digest"])
        print(f"recorded signal: accept (fingerprint={metadata['fingerprint']!r})")

    return 0


def _cmd_reject_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    if not patch_file.is_file():
        print(f"error: no such patch file: {patch_file}", file=sys.stderr)
        return 1

    metadata = _load_patch_metadata(patch_file)
    if metadata is not None:
        _signal_store(root).record(metadata["fingerprint"], Signal.REJECT, metadata["digest"])
        print(f"recorded signal: reject (fingerprint={metadata['fingerprint']!r})")
        _metadata_path(patch_file).unlink(missing_ok=True)
    else:
        print("no fingerprint metadata found alongside this patch — signal not recorded")

    patch_file.unlink()
    print(f"discarded {patch_file}")
    return 0


def _cmd_ignore_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    if not patch_file.is_file():
        print(f"error: no such patch file: {patch_file}", file=sys.stderr)
        return 1

    metadata = _load_patch_metadata(patch_file)
    if metadata is None:
        print("no fingerprint metadata found alongside this patch — signal not recorded")
        return 0

    _signal_store(root).record(metadata["fingerprint"], Signal.IGNORE, metadata["digest"])
    print(f"recorded signal: ignore (fingerprint={metadata['fingerprint']!r})")
    print(f"{patch_file} left on disk, unapplied")
    return 0


def _cmd_signals(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config = load_config(root)
    store = _signal_store(root)
    records = store.all()

    if not records:
        print("no signals recorded yet")
        return 0

    policy = InterruptionPolicy(
        store,
        threshold=config.interruption_threshold,
        min_signals=config.interruption_min_signals,
    )

    fingerprints = sorted({record.fingerprint for record in records})
    for fingerprint in fingerprints:
        history = store.for_fingerprint(fingerprint)
        accepts = sum(1 for record in history if record.signal is Signal.ACCEPT)
        rejects = sum(1 for record in history if record.signal is Signal.REJECT)
        ignores = sum(1 for record in history if record.signal is Signal.IGNORE)
        decision = policy.evaluate_fingerprint(fingerprint)
        verdict = "would surface" if decision.interrupt else "stays silent"
        print(
            f"{fingerprint}: {accepts} accept(s), {rejects} reject(s), "
            f"{ignores} ignore(s) -> score={decision.score:.2f}, {verdict}"
        )
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
    watch_parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="sync change sets into the shadow worktree but don't run checks",
    )
    watch_parser.set_defaults(func=_cmd_watch)

    apply_parser = subparsers.add_parser(
        "apply-ghost", help="apply a verified ghost patch to the live tree (records an accept signal)"
    )
    apply_parser.add_argument("patch_file")
    apply_parser.add_argument("path", nargs="?", default=".")
    apply_parser.set_defaults(func=_cmd_apply_ghost)

    reject_parser = subparsers.add_parser(
        "reject-ghost", help="discard a saved ghost patch (records a reject signal)"
    )
    reject_parser.add_argument("patch_file")
    reject_parser.add_argument("path", nargs="?", default=".")
    reject_parser.set_defaults(func=_cmd_reject_ghost)

    ignore_parser = subparsers.add_parser(
        "ignore-ghost", help="leave a saved ghost patch unapplied (records an ignore signal)"
    )
    ignore_parser.add_argument("patch_file")
    ignore_parser.add_argument("path", nargs="?", default=".")
    ignore_parser.set_defaults(func=_cmd_ignore_ghost)

    signals_parser = subparsers.add_parser(
        "signals", help="show recorded accept/reject/ignore history and current policy scores"
    )
    signals_parser.add_argument("path", nargs="?", default=".")
    signals_parser.set_defaults(func=_cmd_signals)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
