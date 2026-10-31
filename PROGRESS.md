# ShadowCI — Build Progress

## Vision

ShadowCI is an ambient coding agent that watches your code while you work and
prepares verified bug fixes without ever touching your working tree.

It watches file saves, terminal/test output, and idle time, mirrors your
uncommitted changes into a shadow git worktree, and decides when a change set
is complete (debounce + syntax/compile checks + idle detection) so it never
chases half-typed code. It runs deterministic checks first — tests, type
checker, linters, targeted checks on the functions you just changed — and
only then falls back to an LLM. For a real failure, it writes a failing test,
fixes the bug in the shadow worktree, confirms nothing else regresses, and
surfaces a ghost patch with an evidence card that applies to your live files
in one keystroke, rebased onto whatever you've edited meanwhile.

Silence is the default: an interruption policy learns from accept / reject /
ignore signals to keep false interruptions low. ShadowCI ships a terminal
bug-radar view and an evaluation harness that replays recorded coding
sessions with injected bugs and reports detection latency, precision, accept
rate, and false interruptions. LLM access is a pluggable provider: a scripted
deterministic policy for tests and CI, headless `claude -p` or a local model
for demos — no paid API key required to run or evaluate the system.

## Architecture

```
                         ┌─────────────────────┐
  live working tree  ──▶ │   FileWatcher        │  save/modify/delete events
  (your editor)          └──────────┬───────────┘
                                     ▼
                         ┌─────────────────────┐
                         │  ChangeSetDetector   │  debounce + idle + syntax
                         │  (never half-typed)  │  check → stable ChangeSet
                         └──────────┬───────────┘
                                     ▼
                         ┌─────────────────────┐
                         │  ShadowWorktree      │  mirrors live file bytes
                         │  (separate git       │  into an isolated git
                         │   worktree)          │  worktree — live tree is
                         └──────────┬───────────┘  never written to
                                     ▼
                         ┌─────────────────────┐
                         │ Deterministic Checks │  tests / type checker /
                         │ (cheap, no LLM cost) │  linters / targeted tests
                         └──────────┬───────────┘
                             pass │   │ fail
                     (stay silent)│   ▼
                                  │ ┌─────────────────────┐
                                  │ │   LLMProvider        │  pluggable:
                                  │ │ (write failing test, │  scripted / claude -p /
                                  │ │  propose fix, verify)│  local model
                                  │ └──────────┬───────────┘
                                  │            ▼
                                  │ ┌─────────────────────┐
                                  │ │   GhostPatch +       │  diff + evidence
                                  │ │   EvidenceCard       │  card, rebased onto
                                  │ └──────────┬───────────┘  current live tree
                                  │            ▼
                                  │ ┌─────────────────────┐
                                  └▶│ InterruptionPolicy   │  learns from
                                    │ (silence by default) │  accept/reject/ignore
                                    └──────────┬───────────┘
                                                ▼
                                    ┌─────────────────────┐
                                    │  Bug-Radar (TUI) +   │  one-keystroke apply
                                    │  one-keystroke apply │
                                    └─────────────────────┘
```

Separately, an **evaluation harness** replays recorded coding sessions
(timestamped file-save sequences) with synthetically injected bugs through
the same pipeline and reports: detection latency, precision, accept rate,
and false-interruption rate — so the interruption policy and detection
quality can be tuned and regression-tested without a human in the loop.

### Module layout (`src/shadowci/`)

- `config.py` — `ShadowCIConfig`: debounce/idle timings, watched roots, shadow
  worktree location, provider selection. Loaded from `shadowci.toml` with
  sane defaults.
- `events.py` — core data model: `FileChangeEvent`, `ChangeSet`, `CheckResult`,
  `GhostPatch`, `EvidenceCard`, `Signal` (accept/reject/ignore).
- `watcher.py` — `FileWatcher` abstract interface + `PollingFileWatcher`
  implementation (dependency-free, deterministic, easy to test headlessly).
  A `watchdog`-backed watcher can be swapped in later behind the same
  interface without touching downstream code.
- `shadow_worktree.py` — `ShadowWorktree`: creates/reuses a real `git
  worktree`, mirrors live-tree file bytes into it on sync, and never writes
  back to the live tree.
- `changeset.py` — `ChangeSetDetector`: buffers a burst of `FileChangeEvent`s,
  applies a debounce window, an idle-time threshold, and a syntax/compile
  check (`ast.parse`/`py_compile`) before promoting a buffer to a stable
  `ChangeSet`. Incomplete/unparsable edits are never promoted.
- `providers/base.py` — `LLMProvider` abstract interface (`propose_fix`,
  `write_regression_test`, …).
- `providers/scripted.py` — `ScriptedProvider`: deterministic, canned
  responses for tests/CI/demos — no network access, no API key.
- `providers/prompts.py` — shared prompt text for the model-backed
  providers (one place to tune "write a regression test" / "propose a fix").
- `providers/claude_cli.py` — `ClaudeCLIProvider`: shells out to headless
  `claude -p <prompt>` so ShadowCI can get a real model's output with no
  SDK dependency and no API key of its own.
- `providers/local.py` — `LocalModelProvider`: talks to a local model
  server (Ollama's `/api/generate` by default) over HTTP.
- `providers/__init__.py` — `create_provider(name)` factory: builds the
  provider named in `ShadowCIConfig.provider`, raising `ValueError` on a
  typo instead of silently falling back.
- `checks/base.py` — `CheckRunner` interface + `run_subprocess` helper
  shared by every runner.
- `checks/pytest_runner.py` — `PytestRunner`: runs the full suite; "no
  tests collected" counts as a pass, not a failure.
- `checks/type_checker.py` — `MypyRunner`: runs `mypy`, skipping (pass)
  when it isn't installed in the project.
- `checks/lint_runner.py` — `RuffRunner`: runs `ruff check`, same
  skip-when-absent behavior.
- `checks/targeted.py` — `find_targeted_tests` / `TargetedTestRunner`: maps
  a changed file to the test file that plausibly covers it (by name
  convention or by reference), so most change sets only run a handful of
  tests instead of the whole suite.
- `checks/suite.py` — `build_check_runners` / `run_checks`: picks targeted
  tests when a match exists, else the full suite, then runs type checker
  and linter after.
- `escalation.py` — `EscalationPolicy`: calls an `LLMProvider` only when at
  least one `CheckResult` in a batch failed; a clean run never costs a
  model call.
- `ghost.py` — `GhostPatchBuilder`: turns an escalation's regression test
  and proposed fix into a verified `GhostPatch`. Writes the test into the
  shadow worktree and confirms it fails first (or refuses — an unverified
  test proves nothing); applies the fix with `git apply`; re-runs the full
  suite (pytest + mypy + ruff) to confirm nothing else regressed; then
  assembles a single diff (fix + new test file). `apply_to_live_tree` /
  `check_applies_to_live_tree` apply that diff to the live tree with
  `git apply`, which rebases onto edits made there in the meantime by
  matching on context lines — it only refuses when the user's own edits
  overlap the exact lines being patched, instead of silently clobbering
  them. `GhostPatchError` covers every verification/apply failure mode.
- `signals.py` — `SignalStore`: appends/loads `SignalRecord`s (fingerprint,
  `Signal`, patch digest, timestamp) to/from a flat JSON file under
  `.shadowci/signals.json` — the durable history the interruption policy
  scores against.
- `interruption.py` — `fingerprint_for_patch`: a stable key for "this kind
  of fix" built from the checks that originally failed (recorded on
  `EvidenceCard.originally_failed_checks`, added in Phase 4). policy
  starts biased toward silence — a fingerprint with no history, or fewer
  than `min_signals` records, never interrupts; once there's enough,
  `score = (accepts - rejects) / total` and the patch is surfaced only if
  that score clears `threshold`. `ignore` signals count toward the sample
  size but don't push the score either way.
- `pipeline.py` — the UI-free core the rest of the system is built on:
  `ghost_patch_dir` / `signal_store` / `metadata_path` / `load_patch_metadata`
  / `save_ghost_patch` (the `.shadowci/ghost/*.patch` + `.json` sidecar
  layout), `run_checks_and_maybe_escalate` (checks → escalate → verify →
  save, as a `PipelineOutcome` instead of print statements — the exact
  logic `watch` used to inline), and `accept_patch` / `reject_patch` /
  `ignore_patch` (apply-to-live-tree-or-discard + record the signal, as an
  `ActionResult`). Both `cli.py` (printing the result) and `radar.py` /
  `tui.py` (rendering the result) call into this one copy — `cli.py`'s
  `apply-ghost` and the Bug-Radar `a` keybinding are the same function
  call underneath. `accept_patch` now also removes the patch + its
  metadata sidecar once applied, so an accepted patch stops showing up as
  "pending" anywhere (CLI or dashboard) — a gap Phase 4 left open since
  nothing surfaced the pending list as a first-class view until now.
- `radar.py` — the Bug-Radar's state layer. `RadarState` is one snapshot
  of the whole pipeline (recent file events, pending change-set count,
  last check results, pending ghost patches with a fresh
  `InterruptionDecision` each, a bounded scrolling log).
  `scan_pending_patches` rebuilds the pending-patch list from
  `.shadowci/ghost/*.patch` + sidecar on every call — rescanned from disk,
  not cached, so a patch handled from another terminal (plain
  `apply-ghost` while the dashboard is open) shows up correctly on the
  next render with no shared state to keep in sync. `RadarEngine` wraps
  the same `PollingFileWatcher` / `ChangeSetDetector` / `ShadowWorktree`
  `watch` uses; `.step()` drives one poll cycle and returns a `RadarState`
  instead of printing; `.note()` / `.refresh_patches()` give instant
  feedback right after a keybinding without waiting for the next poll.
  `render_lines(state, selected)` turns a snapshot into plain text with no
  `curses` dependency, so the dashboard's content is unit-testable as
  plain text.
- `tui.py` — the thin `curses` loop over `radar.py`: polls
  `RadarEngine.step()` on a timer (`stdscr.timeout`), renders
  `radar.render_lines()` each frame, and turns `a`/`r`/`i` keypresses into
  `pipeline.accept_patch` / `reject_patch` / `ignore_patch` calls on the
  selected pending patch — `j`/`k` move the selection, `q` quits.
  `_handle_key` and `_render` take a duck-typed window argument so they're
  testable without a real terminal (no `curses.wrapper`/`initscr` needed);
  only `run()` touches real `curses`.
- `cli.py` — `shadowci` command group: `init`, `status`, `watch` (runs
  deterministic checks after every stable change set, escalates on
  failure, builds + saves a ghost patch via `GhostPatchBuilder` when the
  escalation's fix actually verifies — alongside a `.json` metadata
  sidecar carrying the patch's fingerprint — then reports the
  `InterruptionPolicy`'s verdict for it; `watch --skip-checks` opts out of
  all of it), `radar` (launches the Bug-Radar `curses` dashboard over the
  same pipeline — `tui.run`), `apply-ghost <patch-file>` (applies a saved
  ghost patch to the live tree and records an `accept` signal when a
  metadata sidecar is present), `reject-ghost <patch-file>` (discards the
  patch, records `reject`), `ignore-ghost <patch-file>` (leaves the patch
  on disk, records `ignore`), `signals` (prints per-fingerprint
  accept/reject/ignore counts and the policy's current verdict for each),
  and `evaluate` (replays the evaluation harness's built-in scenario suite
  — or a single saved scenario file via `--scenario` — and prints its
  report; `--save-dir` dumps the chosen scenario(s) to disk instead of
  running them; `--repeat N` runs the interruption-policy convergence
  replay instead and reports how its verdict evolved).
  `watch` and `apply-ghost`/`reject-ghost`/`ignore-ghost` are now thin
  printers over `pipeline.py`'s functions rather than owning the logic
  themselves.
- `__main__.py` — `python -m shadowci` entry point.
- `evaluation/session.py` — `RecordedEdit` / `RecordedSession`: a
  replayable session format (starting files + timestamped edits, offsets
  in simulated seconds rather than wall-clock time) with JSON
  round-tripping.
- `evaluation/scenarios.py` — `Scenario`: a `RecordedSession` plus ground
  truth (`bug_expected`, and for bug scenarios the regression test + fix
  diff a correct response looks like). `built_in_scenarios()` is the
  five-scenario suite `shadowci evaluate` replays by default.
- `evaluation/harness.py` — `run_scenario` / `run_suite`: replay scenarios
  through the real pipeline in a real temp git repo + shadow worktree on a
  simulated clock, and aggregate into an `EvaluationReport` (precision,
  false-interruption rate, accept rate, mean detection latency).
  `run_convergence`: replays one bug-and-fix scenario repeatedly in one
  project, accepting every verified fix, to show `InterruptionPolicy`'s
  verdict converge from silent to surfacing as accept history accumulates.

## Phased Build Plan

- [x] **Phase 1 — Scaffold & Core Foundation**
  - Project scaffold (`pyproject.toml`, `src/` layout, packaging, pytest config)
  - Core event/data models (`events.py`)
  - Config loading (`config.py`)
  - Dependency-free polling `FileWatcher`
  - `ChangeSetDetector` with debounce + idle + syntax-check gating
  - `ShadowWorktree` manager backed by real `git worktree`
  - `LLMProvider` interface + `ScriptedProvider` stub
  - `shadowci init` / `status` / `watch` CLI
  - Full unit test coverage for every module above, all green

- [x] **Phase 2 — Deterministic Checks & LLM Escalation**
  - Pluggable check runners: `PytestRunner`, `MypyRunner`, `RuffRunner`,
    each degrading to a trivial pass when the tool isn't installed or
    there's nothing to check
  - "Targeted checks" — `find_targeted_tests` maps a changed source file to
    the test file that plausibly covers it (name convention or reference),
    so `watch` runs a handful of tests instead of the full suite when it can
  - `ClaudeCLIProvider` (headless `claude -p`) and `LocalModelProvider`
    (Ollama-style HTTP server) behind the same `LLMProvider` interface,
    plus a `create_provider(name)` factory
  - `EscalationPolicy` — calls the configured `LLMProvider` only when a
    `CheckResult` batch has an actual failure; `shadowci watch` now runs
    checks after every stable change set and prints the escalation
    (regression test + proposed fix) when one fires; `--skip-checks` opts
    back out
  - Full unit test coverage for every module above, all green

- [x] **Phase 3 — Ghost Patches**
  - LLM-driven failing-test authoring in the shadow worktree, with the test
    required to actually reproduce the failure before anything proceeds
  - Fix generation (`git apply` inside the shadow worktree) + full
    regression confirmation (pytest + mypy + ruff all green afterward)
  - `GhostPatch` + `EvidenceCard` construction from a real verified diff
  - Rebase-onto-live-tree-edits before offering a patch (`git apply`'s
    context-line matching tolerates unrelated edits, refuses on overlap)
  - One-command apply-to-live-files flow (`shadowci apply-ghost`)
  - Full unit test coverage for every module above, all green

- [x] **Phase 4 — Interruption Policy**
  - `Signal` (accept/reject/ignore) recording via `SignalStore`, a durable
    JSON history under `.shadowci/signals.json`
  - `EvidenceCard.originally_failed_checks` + `fingerprint_for_patch` give
    every ghost patch a stable key for "this kind of fix"
  - `InterruptionPolicy` — a deterministic scoring model, silent by
    default (no/too-little history never interrupts), that scores
    `(accepts - rejects) / total` against recorded history per fingerprint
  - `watch` reports the policy's verdict (surface vs. stay silent) for
    every verified ghost patch, alongside the saved patch + metadata
    sidecar; `apply-ghost` / `reject-ghost` / `ignore-ghost` are the CLI
    surface that records each signal; `signals` reports the running tally
    and current verdict per fingerprint — this doubles as evaluating the
    policy against recorded history
  - Full unit test coverage for every module above, all green

- [x] **Phase 5 — Bug-Radar TUI**
  - `pipeline.py`: extracted the check-escalate-verify-save pipeline and
    the ghost-patch disk layout (`.shadowci/ghost/*.patch` + `.json`
    sidecar, `.shadowci/signals.json`) out of `cli.py` into UI-free
    functions returning structured results, so a second driver (the TUI)
    doesn't duplicate that logic — only renders it differently
  - `radar.py`: `RadarState` snapshots (watched files, in-flight change
    sets, live check status, pending ghost patches with a live
    `InterruptionDecision` each) driven by `RadarEngine.step()`, plus a
    pure `render_lines()` with no `curses` dependency
  - `tui.py`: a `curses` dashboard over `radar.py` — `a`/`r`/`i` accept/
    reject/ignore the selected pending patch through the exact same
    `pipeline.accept_patch` / `reject_patch` / `ignore_patch` the
    `apply-ghost` / `reject-ghost` / `ignore-ghost` CLI commands call;
    `j`/`k` move the selection, `q` quits
  - `shadowci radar` launches it; `shadowci watch`'s plain-text mode is
    unchanged and still what the test suite and CI screenshots exercise
    headlessly
  - Full unit test coverage for every module above, all green (129 tests)

- [x] **Phase 6 — Evaluation Harness**
  - `evaluation/session.py`: `RecordedEdit` / `RecordedSession` — a
    replayable session format (starting files + a flat list of edits,
    each an offset in simulated seconds from session start rather than a
    wall-clock timestamp) with JSON round-tripping
    (`save_session`/`load_session`)
  - `evaluation/scenarios.py`: `Scenario` pairs a `RecordedSession` with
    ground truth — `bug_expected`, and for bug scenarios the exact
    regression test + fix diff a correct response looks like, so the
    harness drives a real `ScriptedProvider` through the real pipeline
    instead of asserting against a mocked one. Five built-in scenarios
    (`built_in_scenarios()`): three synthetic bugs (an off-by-one
    operator, an off-by-one bound, an inverted comparison) and two clean
    edits (a rename, a comment) that should never escalate. Also
    JSON-serializable (`save_scenario`/`load_scenario`) for custom
    scenarios authored outside the built-in suite
  - `evaluation/harness.py`: `run_scenario` replays one scenario's edits
    through the real pipeline (`pipeline.run_checks_and_maybe_escalate`,
    the exact function `watch` and `radar` call) in a real temp git repo
    + shadow worktree, on a simulated clock fed straight to
    `ChangeSetDetector.poll(now=...)` so a whole suite replays in
    milliseconds; `run_suite` aggregates many scenarios into an
    `EvaluationReport` (precision, false-interruption rate, accept rate,
    mean detection latency); `run_convergence` replays the *same*
    bug-and-fix scenario several times in one project, accepting every
    verified fix, to show `InterruptionPolicy`'s verdict start silent
    (below `min_signals`) and converge to surfacing once accept history
    clears the threshold — a concrete, regression-testable demonstration
    of the exact mechanism Phase 4 built
  - `shadowci evaluate`: runs the built-in suite and prints a per-scenario
    verdict (DETECTED/MISSED/SILENT/FALSE INTERRUPTION) plus the
    aggregate report; `--save-dir <dir>` writes the chosen scenario(s) to
    disk as JSON instead of running them; `--scenario <path>` replays a
    single saved scenario file instead of the built-in suite; `--repeat N`
    runs the convergence replay instead and prints how the policy's
    verdict evolved iteration by iteration
  - Full unit test coverage for every module above, all green (149 tests)

## Where to resume

All six planned phases are complete — scaffold, core data models, polling
watcher, change-set detector, shadow worktree manager, the full `checks/`
package (pytest/mypy/ruff/targeted), the `claude-cli`/`local`/`scripted`
providers, `EscalationPolicy`, `GhostPatchBuilder` (verified test + fix +
full-suite confirmation + rebased diff), `SignalStore` +
`InterruptionPolicy` (deterministic, silent-by-default scoring over
recorded accept/reject/ignore history), a `watch` command that runs
checks, escalates on failure, saves a verified ghost patch plus a
fingerprint metadata sidecar, and reports the interruption policy's
verdict for it, `apply-ghost` / `reject-ghost` / `ignore-ghost` /
`signals` to record signals and inspect the running tally, `radar` — a
`curses` terminal dashboard (`radar.py` state layer + `tui.py` render
loop) over the exact same pipeline — and now `evaluate`, a third driver
over the same pipeline that replays recorded (optionally synthetic-bug-
injected) sessions on a simulated clock and reports detection latency,
precision, accept rate, and false interruptions with no human in the
loop. See `DAILY_REPORT.md` for the full build summary.

If this project is picked up again, the natural next steps are a richer
`InterruptionPolicy` fingerprint (see the Phase 4 known limitation below —
now directly measurable with `shadowci evaluate`'s harness instead of
guesswork), a `watchdog`-backed `FileWatcher` for lower-latency live use,
and a larger, more varied built-in scenario suite (multi-file change sets,
deletions, scenarios with a verified-but-undesirable fix to exercise real
false interruptions through the interruption policy rather than only
through the deterministic checks).

A known limitation from Phase 3: if a fix applies but the full-suite
regression check fails afterward, the shadow worktree is left with the fix
still applied (only the generated test file is cleaned up) — it'll be
overwritten on the next `sync()` anyway, but it's not an explicit revert.
Worth a cleanup pass if the shadow worktree ever needs to be trusted
mid-failure for something else.

A known limitation from Phase 4: the interruption policy's fingerprint is
only the sorted set of originally-failed check names (e.g.
`"pytest-targeted"` or `"mypy,pytest"`) — it doesn't yet distinguish fixes
by file, project area, or bug shape within the same check. That's a
deliberately small first cut; a richer fingerprint (e.g. including touched
file extensions or paths) would need matching history to actually
accumulate signal per bucket, which isn't worth the complexity until the
Phase 6 evaluation harness can measure whether it actually improves
precision.

STATUS: COMPLETE
