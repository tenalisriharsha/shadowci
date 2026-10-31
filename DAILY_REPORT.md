# ShadowCI — Daily Report

A summary of what was built, how it was verified, what's known to be
limited, and where it could go next. See [PROGRESS.md](PROGRESS.md) for
the detailed, phase-by-phase build log and module layout.

## What was built

ShadowCI is an ambient coding agent that watches a project's file saves,
mirrors uncommitted edits into an isolated git worktree, and runs
deterministic checks there before ever calling an LLM — and never touches
the live working tree except through a single, explicit "apply" command.

**Phase 1 — Foundation.** The core data model (`FileChangeEvent`,
`ChangeSet`, `CheckResult`, `GhostPatch`, `EvidenceCard`, `Signal`), a
dependency-free polling file watcher, a `ChangeSetDetector` that only
promotes a batch of edits once it's debounced, idle, and syntactically
valid, a `ShadowWorktree` manager backed by a real `git worktree`, a
pluggable `LLMProvider` interface with a deterministic `ScriptedProvider`,
and the `init`/`status`/`watch` CLI commands.

**Phase 2 — Deterministic checks & escalation.** Check runners for pytest,
mypy, and ruff, each degrading to a harmless pass when the tool isn't
installed; targeted-test matching so most change sets run a handful of
tests instead of the full suite; `claude-cli` (headless `claude -p`) and
`local` (Ollama-style HTTP) providers behind the same interface; an
`EscalationPolicy` that only calls a provider once a real check fails.

**Phase 3 — Ghost patches.** `GhostPatchBuilder` turns an escalation into a
*verified* fix entirely inside the shadow worktree: the provider's
regression test must actually reproduce the failure, the proposed fix must
apply cleanly, and the full suite must stay green afterward. Only then is
a single diff (fix + new test file) assembled and saved — and
`shadowci apply-ghost` applies it to the live tree in one command, which
rebases naturally onto whatever's changed there since (via `git apply`'s
context-line matching).

**Phase 4 — Interruption policy.** Every `apply-ghost` / `reject-ghost` /
`ignore-ghost` records a signal, grouped by a fingerprint built from which
checks originally failed. `InterruptionPolicy` is silent by default — a
fingerprint with no or too little history never interrupts — and only
starts surfacing a kind of fix once its accept history clears a threshold.
`shadowci signals` reports the running tally and verdict per fingerprint.

**Phase 5 — Bug-Radar.** The check → escalate → verify → save pipeline was
extracted out of the CLI into `pipeline.py`, UI-free, so a second driver
could reuse it without duplicating the logic. `shadowci radar` is a
`curses` terminal dashboard over that same pipeline: watched files, live
check status, and pending ghost patches with a live interruption-policy
verdict each, handled with `a`/`r`/`i` keybindings that call the exact same
`pipeline.accept_patch`/`reject_patch`/`ignore_patch` the plain CLI uses.

**Phase 6 — Evaluation harness.** A third driver over the same pipeline,
with no human and no real wall-clock time: `evaluation/session.py` defines
a replayable session format (starting files + a flat list of edits, each
timestamped in simulated seconds), `evaluation/scenarios.py` pairs a
session with ground truth — a synthetic bug plus the regression test and
fix diff a correct response looks like, or a clean edit that should never
escalate — and `evaluation/harness.py` replays those scenarios through the
*real* pipeline (real git repo, real shadow worktree, real
`GhostPatchBuilder` verification) on a simulated clock fed straight into
`ChangeSetDetector.poll(now=...)`, so a whole suite replays in
milliseconds. `shadowci evaluate` reports detection latency, precision,
false-interruption rate, and accept rate across the built-in five-scenario
suite, and `--repeat N` replays one bug-and-fix scenario repeatedly,
accepting every verified fix, to show `InterruptionPolicy`'s verdict
converge from silent to surfacing as history accumulates — a concrete,
regression-testable demonstration of the exact mechanism Phase 4 built.

## Test results

149 tests, all passing, covering every module from the watcher and
change-set detector through the shadow worktree, checks, providers,
escalation, ghost-patch verification, signals, interruption policy, radar
dashboard, CLI, and the Phase 6 evaluation harness. No skips, no known
flaky tests as of this report (`run_convergence`'s repeated same-file,
same-size rewrites did surface one genuine bytecode-cache race during
development — fixed by clearing `__pycache__` before each replay
iteration — see "Known limitations" below for the underlying cause).

```
pytest -q
149 passed
```

## Known limitations

- **Ghost patch cleanup on regression failure.** If a fix applies but the
  post-fix full-suite check fails, the shadow worktree is left with the
  fix still applied (only the generated test file is removed). It's
  overwritten on the next `sync()` regardless, but it isn't an explicit
  revert — worth a cleanup pass if the shadow worktree ever needs to be
  trusted mid-failure for something else.
- **Interruption policy fingerprint is coarse.** It's just the sorted set
  of originally-failed check names (e.g. `"pytest-targeted"` or
  `"mypy,pytest"`) — it doesn't distinguish fixes by file, project area, or
  bug shape within the same check. `shadowci evaluate` now gives a way to
  actually measure whether a richer fingerprint would improve precision,
  rather than guessing.
- **Compiled-bytecode staleness under a simulated clock.** Python's
  per-file compiled-bytecode cache (`__pycache__`) is normally invalidated
  by a source file's mtime and size. The evaluation harness's
  `run_convergence` rewrites the same module path between a same-length
  "buggy" and "fixed" version, over and over, faster than the filesystem's
  mtime resolution can tell apart — without clearing `__pycache__` before
  each iteration, a stale compiled module can get served, randomly passing
  or failing a check against the wrong source. This is purely an artifact
  of replaying unrealistically fast inside one process; real usage writes
  files on human timescales and never hits it.
- **Scenario suite is intentionally small.** The built-in suite covers
  three single-file synthetic bugs and two clean edits. It doesn't yet
  cover multi-file change sets, deletions, or a fix that verifies but is
  semantically undesirable (which would be the harness's way of exercising
  a genuine false interruption through the interruption policy itself,
  rather than only through a spurious deterministic-check failure).
- **Polling file watcher.** `PollingFileWatcher` snapshots mtimes/sizes on
  every poll rather than using OS-level file events — simple and
  dependency-free, but less immediate than a `watchdog`-backed watcher
  would be for real interactive use. The `FileWatcher` interface was kept
  abstract from Phase 1 specifically so a different implementation can
  drop in later without touching anything downstream.

## Ideas for future work

- A `watchdog`-backed `FileWatcher` for lower-latency live use, behind the
  existing abstract interface.
- A richer `InterruptionPolicy` fingerprint (touched file extensions or
  paths, not just which checks failed), evaluated against
  `shadowci evaluate`'s harness before and after to confirm it actually
  improves precision rather than just adding complexity.
- A larger, more varied scenario suite: multi-file change sets, file
  deletions, and at least one "verified but undesirable fix" scenario to
  exercise real false interruptions through the policy layer.
- A `watchdog`-free but push-based watcher mode, or an editor plugin, so
  `shadowci watch` doesn't need a polling loop at all for supported editors.
