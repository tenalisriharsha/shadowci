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
`ignore-ghost` record the accept/reject/ignore signal that trains it.
Phase 5 adds `radar`, a terminal dashboard over the same pipeline
(`pipeline.py`) for an ambient view instead of a scrolling log. Phase 6
adds `evaluate`, a third driver over the same pipeline that replays
recorded (optionally synthetic-bug-injected) sessions on a simulated clock
and reports detection latency, precision, accept rate, and false
interruptions, with no human in the loop.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from shadowci import pipeline
from shadowci.changeset import ChangeSetDetector
from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.events import ChangeSet, CheckResult, Signal
from shadowci.interruption import InterruptionPolicy
from shadowci.providers import create_provider
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


def _run_checks_and_maybe_escalate(
    changeset: ChangeSet, worktree: ShadowWorktree, config: ShadowCIConfig
) -> None:
    outcome = pipeline.run_checks_and_maybe_escalate(changeset, worktree, config, create_provider)

    for result in outcome.check_results:
        _print_check_result(result)

    if not outcome.failing_checks:
        return

    if outcome.unknown_provider_error is not None:
        print(f"  skipping escalation: {outcome.unknown_provider_error}", file=sys.stderr)
        return

    print(
        f"  escalating to provider '{outcome.provider_name}' "
        f"({len(outcome.failing_checks)} check(s) failed)"
    )

    if outcome.provider_error is not None:
        print(f"  provider error: {outcome.provider_error}", file=sys.stderr)
        return

    if outcome.regression_test is None:
        return

    print("  regression test:")
    for line in outcome.regression_test.splitlines():
        print(f"    {line}")
    print("  proposed fix:")
    for line in outcome.proposed_fix.splitlines():
        print(f"    {line}")

    print("  verifying fix in shadow worktree...")

    if outcome.ghost_patch_error is not None:
        print(f"  could not verify a ghost patch: {outcome.ghost_patch_error}", file=sys.stderr)
        return

    if outcome.ghost_patch is None or outcome.ghost_patch_path is None:
        return

    print(f"  ghost patch verified: {outcome.ghost_patch.evidence.summary}")
    print(f"  saved to {outcome.ghost_patch_path}")

    decision = outcome.decision
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
    print(f"  apply with: shadowci apply-ghost {outcome.ghost_patch_path}")


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


def _cmd_radar(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config = load_config(root)
    if args.interval is not None:
        config.poll_interval_seconds = args.interval

    from shadowci import tui

    return tui.run(config, skip_checks=args.skip_checks, max_cycles=args.max_cycles)


def _cmd_apply_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    result = pipeline.accept_patch(root, patch_file)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_reject_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    result = pipeline.reject_patch(root, patch_file)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_ignore_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = Path(args.patch_file).resolve()

    result = pipeline.ignore_patch(root, patch_file)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_signals(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    config = load_config(root)
    store = pipeline.signal_store(root)
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


def _format_metric(value: float | None, suffix: str = "", na: str = "n/a") -> str:
    return na if value is None else f"{value:.2f}{suffix}"


def _cmd_evaluate(args: argparse.Namespace) -> int:
    from shadowci.evaluation import harness, scenarios as scenario_lib

    if args.scenario:
        chosen = (scenario_lib.load_scenario(Path(args.scenario)),)
    else:
        chosen = scenario_lib.built_in_scenarios()

    if args.save_dir:
        save_root = Path(args.save_dir)
        save_root.mkdir(parents=True, exist_ok=True)
        for scenario in chosen:
            path = save_root / f"{scenario.session.name}.json"
            scenario_lib.save_scenario(scenario, path)
            print(f"wrote {path}")
        return 0

    if args.repeat:
        bug_scenarios = [scenario for scenario in chosen if scenario.bug_expected]
        if not bug_scenarios:
            print("error: no bug scenario available to repeat", file=sys.stderr)
            return 1
        scenario = bug_scenarios[0]
        convergence = harness.run_convergence(scenario, repeats=args.repeat)

        print(f"replaying '{scenario.session.name}' {args.repeat} time(s), accepting every verified fix:")
        for index, iteration in enumerate(convergence.iterations, start=1):
            if iteration.decision is None:
                print(f"  iteration {index}: not verified")
                continue
            verdict = "surfaces" if iteration.decision.interrupt else "stays silent"
            print(
                f"  iteration {index}: verified, policy {verdict} "
                f"(score={iteration.decision.score:.2f} over {iteration.decision.sample_size} past signal(s))"
            )

        first = convergence.first_interrupt_iteration
        if first is None:
            print("the policy never chose to surface this kind of fix over this run")
        else:
            print(f"the policy started surfacing this kind of fix from iteration {first} onward")
        return 0

    report = harness.run_suite(chosen)

    for result in report.results:
        name = result.scenario.session.name
        expectation = "bug" if result.scenario.bug_expected else "clean"
        if result.scenario.bug_expected:
            verdict = "DETECTED" if result.true_positive else "MISSED"
        else:
            verdict = "FALSE INTERRUPTION" if result.false_interruption else "SILENT (correct)"
        latency = (
            f", latency={result.detection_latency_seconds:.3f}s"
            if result.detection_latency_seconds is not None
            else ""
        )
        print(f"{name} [{expectation}]: {verdict}{latency}")

    print()
    print(f"scenarios run           : {len(report.results)}")
    print(f"true positives          : {report.true_positives}")
    print(f"false negatives         : {report.false_negatives}")
    print(f"false interruptions     : {report.false_interruptions}")
    print(f"precision               : {_format_metric(report.precision)}")
    print(f"false interruption rate : {_format_metric(report.false_interruption_rate)}")
    print(f"accept rate             : {_format_metric(report.accept_rate)}")
    print(
        "mean detection latency  : "
        f"{_format_metric(report.mean_detection_latency_seconds, suffix='s')}"
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

    radar_parser = subparsers.add_parser(
        "radar", help="launch the Bug-Radar terminal dashboard"
    )
    radar_parser.add_argument("path", nargs="?", default=".")
    radar_parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="stop after this many poll cycles (default: run until 'q')",
    )
    radar_parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="override the poll interval in seconds",
    )
    radar_parser.add_argument(
        "--skip-checks",
        action="store_true",
        help="sync change sets into the shadow worktree but don't run checks",
    )
    radar_parser.set_defaults(func=_cmd_radar)

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

    evaluate_parser = subparsers.add_parser(
        "evaluate",
        help="replay recorded (optionally bug-injected) sessions through the real pipeline",
    )
    evaluate_parser.add_argument(
        "--scenario",
        default=None,
        help="path to a saved scenario JSON file to replay instead of the built-in suite",
    )
    evaluate_parser.add_argument(
        "--save-dir",
        default=None,
        help="write the chosen scenario(s) to this directory as JSON instead of running them",
    )
    evaluate_parser.add_argument(
        "--repeat",
        type=int,
        default=None,
        help=(
            "replay one bug scenario this many times, accepting every verified fix, "
            "and report how the interruption policy's verdict evolves"
        ),
    )
    evaluate_parser.set_defaults(func=_cmd_evaluate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
