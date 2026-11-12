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

from shadowci import doctor, interruption, pipeline
from shadowci import pause as pause_module
from shadowci.changeset import ChangeSetDetector
from shadowci.config import DEFAULT_CONFIG_FILENAME, ShadowCIConfig, load_config
from shadowci.events import ChangeSet, CheckResult, Signal
from shadowci.interruption import InterruptionPolicy
from shadowci.providers import create_provider
from shadowci.shadow_worktree import ShadowWorktree, ShadowWorktreeError
from shadowci.watcher import PollingFileWatcher

REJECT_REASONS = ("intentional-change", "wrong-fix", "not-now")


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
    print(f"pause            : {_describe_pause_state(root)}")
    return 0


def _describe_pause_state(root: Path) -> str:
    state = pause_module.current_pause(root)
    if state is None:
        return "not paused"
    if state.until is None:
        return "paused indefinitely (resume with: shadowci resume)"
    remaining = max(0.0, state.until - time.time())
    return f"paused, {remaining:.0f}s remaining (resume with: shadowci resume)"


def _cmd_pause(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    duration_seconds = None
    if args.duration is not None:
        try:
            duration_seconds = pause_module.parse_duration(args.duration)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    pause_module.pause(root, duration_seconds)
    if duration_seconds is None:
        print("paused: no fix will be generated until `shadowci resume`")
    else:
        print(f"paused for {args.duration}: no fix will be generated until it expires or `shadowci resume`")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    if pause_module.resume(root):
        print("resumed: escalation to the model is active again")
    else:
        print("not paused")
    return 0


def _print_doctor_check(check: doctor.DoctorCheck) -> None:
    print(f"[{check.status}] {check.name}: {check.message}")
    if check.status != doctor.OK and check.fix_hint:
        print(f"  {check.fix_hint}")


def _cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()

    if args.fix:
        for line in doctor.apply_fixes(root):
            print(line)

    checks = doctor.run_checks(root)
    for check in checks:
        _print_doctor_check(check)

    return 1 if any(check.status == doctor.FAIL for check in checks) else 0


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

    if outcome.paused_message is not None:
        print(f"  {outcome.paused_message}")
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

    if outcome.question is not None:
        question = outcome.question
        print(f"  {question.message}")
        print(f"  keep old behaviour: {question.keep_old_behavior_path.stem}")
        print(f"  update the test: {question.update_test_path.stem}")
        print("  choose with: shadowci apply-ghost <digest>")
        return

    if outcome.ghost_patch_error is not None:
        print(f"  could not verify a ghost patch: {outcome.ghost_patch_error}", file=sys.stderr)
        return

    if outcome.ghost_patch is None or outcome.ghost_patch_path is None:
        return

    print(f"  ghost patch verified: {outcome.ghost_patch.evidence.summary}")
    print(f"  saved to {outcome.ghost_patch_path}")
    for retired_name in outcome.retired_ghost_patches:
        print(f"  retired superseded ghost patch: {retired_name}")

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
    pipeline.ensure_mirrored_at_start(worktree, config)

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
            sync_result = pipeline.sync_shadow_for_changeset(changeset, worktree, config)
            if sync_result.skipped_in_progress is not None:
                print(f"  skipping sync: {sync_result.skipped_in_progress}")
            elif sync_result.git_only_change:
                print(f"rebaselined shadow worktree to {sync_result.rebaselined_to}")
            else:
                if sync_result.rebaselined_to is not None:
                    print(f"rebaselined shadow worktree to {sync_result.rebaselined_to}")
                print(
                    f"changeset ready: {len(changeset.paths)} file(s) -> "
                    f"synced {len(sync_result.touched)} into shadow worktree"
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


def _resolve_ghost_target(args: argparse.Namespace, root: Path) -> Path | None:
    """The patch file an apply/reject/ignore-ghost invocation should act on.

    With `--latest`, that's the newest saved patch that still applies to
    the live tree; returns None (and prints why) when there isn't one.
    Without it, falls back to the explicit `patch_file` argument.
    """
    if args.latest:
        patch_file = pipeline.latest_ready_ghost_patch(root)
        if patch_file is None:
            print("no ready ghost patch to apply", file=sys.stderr)
            return None
        return patch_file

    if not args.patch_file:
        print("error: patch_file is required unless --latest is given", file=sys.stderr)
        return None
    return Path(args.patch_file).resolve()


def _cmd_apply_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = _resolve_ghost_target(args, root)
    if patch_file is None:
        return 1

    result = pipeline.accept_patch(root, patch_file)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_reject_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = _resolve_ghost_target(args, root)
    if patch_file is None:
        return 1

    result = pipeline.reject_patch(root, patch_file, reason=args.reason)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_ignore_ghost(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()
    patch_file = _resolve_ghost_target(args, root)
    if patch_file is None:
        return 1

    result = pipeline.ignore_patch(root, patch_file)
    if not result.ok:
        print(f"error: {result.message}", file=sys.stderr)
        return 1

    print(result.message)
    return 0


def _cmd_ghosts(args: argparse.Namespace) -> int:
    root = Path(args.path).resolve()

    if args.clean:
        removed = pipeline.clean_ghost_patches(root)
        if not removed:
            print("no stale ghost patches to remove")
        else:
            for name in removed:
                print(f"removed stale ghost patch: {name}")
        return 0

    listings = pipeline.list_ghost_patches(root)
    if not listings:
        print("no saved ghost patches")
        return 0

    for listing in listings:
        status = "ready" if listing.ready else "stale"
        metadata = listing.metadata or {}
        summary = metadata.get("summary", "(no summary)")
        files = ", ".join(metadata.get("files", [])) or "(unknown files)"
        if metadata.get("kind") == "question":
            label = metadata.get("label", "question")
            group = metadata.get("group", "?")
            print(
                f"{listing.digest}  [{status}]  [question: {label}, group={group}]  "
                f"{summary}  ({files})"
            )
        else:
            print(f"{listing.digest}  [{status}]  {summary}  ({files})")
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
        intentional = sum(
            1 for record in history if record.reason == interruption.INTENTIONAL_CHANGE_REASON
        )
        accepts = sum(1 for record in history if record.signal is Signal.ACCEPT)
        rejects = sum(
            1
            for record in history
            if record.signal is Signal.REJECT
            and record.reason != interruption.INTENTIONAL_CHANGE_REASON
        )
        ignores = sum(1 for record in history if record.signal is Signal.IGNORE)
        decision = policy.evaluate_fingerprint(fingerprint)
        verdict = "would surface" if decision.interrupt else "stays silent"
        intentional_suffix = f", {intentional} intentional-change" if intentional else ""
        print(
            f"{fingerprint}: {accepts} accept(s), {rejects} reject(s), "
            f"{ignores} ignore(s){intentional_suffix} -> score={decision.score:.2f}, {verdict}"
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
    print(f"reverted_user_change    : {report.reverted_user_change}")
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

    pause_parser = subparsers.add_parser(
        "pause", help="hold back escalation to the model while a feature is mid-edit"
    )
    pause_parser.add_argument("path", nargs="?", default=".")
    pause_parser.add_argument(
        "--for",
        dest="duration",
        default=None,
        help="pause for this long, e.g. '30m', '2h', '45s' (default: until `shadowci resume`)",
    )
    pause_parser.set_defaults(func=_cmd_pause)

    resume_parser = subparsers.add_parser("resume", help="resume escalation to the model after a pause")
    resume_parser.add_argument("path", nargs="?", default=".")
    resume_parser.set_defaults(func=_cmd_resume)

    doctor_parser = subparsers.add_parser(
        "doctor", help="diagnose whether ShadowCI can work in this project and how to fix it"
    )
    doctor_parser.add_argument("path", nargs="?", default=".")
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="apply the safe fixes (recreate/rebaseline the shadow worktree, drop stale ghost patches)",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

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
    apply_parser.add_argument("patch_file", nargs="?", default=None)
    apply_parser.add_argument("path", nargs="?", default=".")
    apply_parser.add_argument(
        "--latest",
        action="store_true",
        help="act on the newest saved ghost patch that still applies, instead of naming one",
    )
    apply_parser.set_defaults(func=_cmd_apply_ghost)

    reject_parser = subparsers.add_parser(
        "reject-ghost", help="discard a saved ghost patch (records a reject signal)"
    )
    reject_parser.add_argument("patch_file", nargs="?", default=None)
    reject_parser.add_argument("path", nargs="?", default=".")
    reject_parser.add_argument(
        "--latest",
        action="store_true",
        help="act on the newest saved ghost patch that still applies, instead of naming one",
    )
    reject_parser.add_argument(
        "--reason",
        choices=REJECT_REASONS,
        default=None,
        help=(
            "why this patch was rejected. 'intentional-change' means the fix was fine but your "
            "own edit was deliberate, and is not counted against the fingerprint's score; "
            "'wrong-fix' and 'not-now' are recorded but still count as an ordinary reject"
        ),
    )
    reject_parser.set_defaults(func=_cmd_reject_ghost)

    ignore_parser = subparsers.add_parser(
        "ignore-ghost", help="leave a saved ghost patch unapplied (records an ignore signal)"
    )
    ignore_parser.add_argument("patch_file", nargs="?", default=None)
    ignore_parser.add_argument("path", nargs="?", default=".")
    ignore_parser.add_argument(
        "--latest",
        action="store_true",
        help="act on the newest saved ghost patch that still applies, instead of naming one",
    )
    ignore_parser.set_defaults(func=_cmd_ignore_ghost)

    ghosts_parser = subparsers.add_parser(
        "ghosts", help="list saved ghost patches and whether they still apply"
    )
    ghosts_parser.add_argument("path", nargs="?", default=".")
    ghosts_parser.add_argument(
        "--clean",
        action="store_true",
        help="remove saved ghost patches that no longer apply, without recording a signal",
    )
    ghosts_parser.set_defaults(func=_cmd_ghosts)

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
