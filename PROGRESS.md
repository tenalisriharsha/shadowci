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
- `cli.py` — `shadowci` command group: `init`, `status`, `watch` (now runs
  deterministic checks after every stable change set and escalates on
  failure; `watch --skip-checks` opts back out).
- `__main__.py` — `python -m shadowci` entry point.

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

- [ ] **Phase 3 — Ghost Patches**
  - LLM-driven failing-test authoring in the shadow worktree
  - Fix generation + full regression confirmation inside the shadow worktree
  - `GhostPatch` + `EvidenceCard` construction
  - Rebase-onto-live-tree-edits before offering a patch
  - One-keystroke apply-to-live-files flow

- [ ] **Phase 4 — Interruption Policy**
  - Accept / reject / ignore signal recording
  - Learned scoring model that keeps default behavior silent
  - Policy evaluation against recorded signal history

- [ ] **Phase 5 — Bug-Radar TUI**
  - Terminal dashboard (watched files, in-flight change sets, check status,
    pending ghost patches)

- [ ] **Phase 6 — Evaluation Harness**
  - Session recording/replay format
  - Synthetic bug injection
  - Metrics: detection latency, precision, accept rate, false interruptions
  - Harness report CLI output

## Where to resume

**Next session: start Phase 3 (Ghost Patches).** Phases 1 and 2 are
complete — scaffold, core data models, polling watcher, change-set
detector, shadow worktree manager, the full `checks/` package
(pytest/mypy/ruff/targeted), the `claude-cli`/`local`/`scripted` providers,
`EscalationPolicy`, and a `watch` command that runs checks and escalates on
failure are all implemented and fully tested (see `tests/`).

Right now `EscalationPolicy` only asks a provider for a regression test and
a fix diff and prints them — nothing is applied anywhere. Phase 3 needs to:
take the regression test the provider wrote, actually add it to the shadow
worktree and confirm it reproduces the failure; apply the proposed fix
there too and re-run the full suite to confirm nothing else regressed; only
then construct a `GhostPatch` + `EvidenceCard` (both already modeled in
`events.py`) from a real diff between the shadow worktree and the live
tree; and rebase that diff onto whatever the user has edited in the live
tree meanwhile before it's considered ready to offer. The one-keystroke
apply-to-live-files flow is the last piece of that phase.

STATUS: IN_PROGRESS
