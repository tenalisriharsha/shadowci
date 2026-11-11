"""Issue #38: a failing check should be classified before it's escalated.

Through issue #37, every verified fix is grouped for the interruption
policy by the *check* that failed (`fingerprint_for_patch` returns e.g.
`pytest-targeted`) and nothing else — a patch that silently reverts the
user's own edit and a patch that fixes a genuine regression elsewhere look
identical to the policy as long as the same check failed. These tests pin
down the richer classification issue #38 asks for:

- `regression-elsewhere` — the edited file isn't what the failing test
  covers, and the test passed at HEAD: a real regression, fix with
  confidence.
- `test-edited` — the user edited the failing test file itself: the test
  is the new spec, so a patch may only touch code, never a test file the
  user edited.
- `unclear-behavior-change` — the user edited the code the test covers,
  and the test passed at HEAD: ambiguous, should be questioned (issue #37).
- `pre-existing` — the test already failed at HEAD, before this edit:
  never escalated as a regression of this edit, no patch produced.

The interruption policy's fingerprint becomes `<class>:<checks>` so these
four cases never share signal history just because the same check name
happened to fail.
"""

from __future__ import annotations

import difflib
import json
import subprocess
from pathlib import Path

from shadowci import pipeline
from shadowci.cli import main
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent, Signal
from shadowci.interruption import InterruptionPolicy
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree
from shadowci.signals import SignalStore

_APP_ORIGINAL = "def add(a, b):\n    return a + b\n"
_APP_BROKEN = "def add(a, b):\n    return a + b + 1\n"
_TEST_APP = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
_TEST_APP_EDITED = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 10\n"
_TEST_APP_PRE_EXISTING = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 999\n"


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(path: Path) -> Path:
    """A second, independent git repo (same shape as the `git_repo` fixture)."""
    path.mkdir()
    _run_git(["init", "-q"], cwd=path)
    _run_git(["config", "user.name", "Test User"], cwd=path)
    _run_git(["config", "user.email", "test@example.com"], cwd=path)
    (path / "app.py").write_text(_APP_ORIGINAL)
    _run_git(["add", "app.py"], cwd=path)
    _run_git(["commit", "-q", "-m", "initial commit"], cwd=path)
    return path


def _commit_all(repo: Path, message: str = "setup") -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _setup_simple_repo(repo: Path, test_content: str = _TEST_APP) -> None:
    """Commit `tests/test_app.py` on top of whatever `app.py` the repo already has."""
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_app.py").write_text(test_content)
    _commit_all(repo)


def _edit_and_sync(repo: Path, relative: str, content: str) -> tuple[ShadowWorktree, ChangeSet]:
    live_file = repo / relative
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text(content)
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(_config(repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def _diff(relative: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relative}",
        tofile=f"b/{relative}",
    )
    text = "".join(lines)
    return text if text.endswith("\n") else text + "\n"


def test_edit_to_unrelated_code_that_regresses_a_passing_test_is_regression_elsewhere(
    git_repo: Path,
):
    """Acceptance #1: edited file isn't what the failing test covers, and
    the test passed at HEAD -> `regression-elsewhere`, fixed with confidence."""
    (git_repo / "other.py").write_text("def helper(a, b):\n    return a + b\n")
    (git_repo / "app.py").write_text(
        "from other import helper\n\n\ndef add(a, b):\n    return helper(a, b)\n"
    )
    _setup_simple_repo(git_repo)
    config = _config(git_repo)

    worktree, changeset = _edit_and_sync(
        git_repo, "other.py", "def helper(a, b):\n    return a + b + 1\n"
    )
    fix = _diff(
        "other.py",
        "def helper(a, b):\n    return a + b + 1\n",
        "def helper(a, b):\n    return a + b\n",
    )
    test = "from app import add\n\n\ndef test_shadowci_regression():\n    assert add(2, 3) == 5\n"

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[test], fixes=[fix])
    )

    assert outcome.ghost_patch is not None
    assert outcome.decision is not None
    assert outcome.decision.fingerprint.startswith("regression-elsewhere:")


def test_edit_to_the_code_under_a_passing_test_is_unclear_behavior_change(git_repo: Path):
    """Acceptance #3: edited file is the code this test covers, and the
    test passed at HEAD -> `unclear-behavior-change`."""
    _setup_simple_repo(git_repo)
    config = _config(git_repo)

    worktree, changeset = _edit_and_sync(git_repo, "app.py", _APP_BROKEN)
    fix = _diff("app.py", _APP_BROKEN, _APP_ORIGINAL)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[_TEST_APP], fixes=[fix])
    )

    assert outcome.ghost_patch is not None
    assert outcome.decision is not None
    assert outcome.decision.fingerprint.startswith("unclear-behavior-change:")


def test_edit_to_the_test_itself_with_a_code_only_fix_is_test_edited(git_repo: Path):
    """Acceptance #2: the user edited the failing test file itself ->
    `test-edited`, and a fix that only touches code is saved normally."""
    _setup_simple_repo(git_repo)
    config = _config(git_repo)

    worktree, changeset = _edit_and_sync(git_repo, "tests/test_app.py", _TEST_APP_EDITED)
    fix = _diff("app.py", _APP_ORIGINAL, "def add(a, b):\n    return a + b + 5\n")
    test = "from app import add\n\n\ndef test_shadowci_regression():\n    assert add(2, 3) == 10\n"

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[test], fixes=[fix])
    )

    assert outcome.ghost_patch is not None
    assert outcome.decision is not None
    assert outcome.decision.fingerprint.startswith("test-edited:")
    assert "tests/test_app.py" not in outcome.ghost_patch.diff


def test_fix_that_also_touches_the_edited_test_file_is_rejected(git_repo: Path):
    """Acceptance #2: a proposed fix that touches a test file the user
    edited must never be saved as a verified fix, even if it also
    contains a legitimate code fix and verifies cleanly."""
    _setup_simple_repo(git_repo)
    config = _config(git_repo)

    worktree, changeset = _edit_and_sync(git_repo, "tests/test_app.py", _TEST_APP_EDITED)
    app_fix = _diff("app.py", _APP_ORIGINAL, "def add(a, b):\n    return a + b + 5\n")
    test_fix = _diff(
        "tests/test_app.py",
        _TEST_APP_EDITED,
        "from app import add\n\n\ndef test_app():\n    # tidy\n    assert add(2, 3) == 10\n",
    )
    fix = app_fix + test_fix
    test = "from app import add\n\n\ndef test_shadowci_regression():\n    assert add(2, 3) == 10\n"

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[test], fixes=[fix])
    )

    assert outcome.ghost_patch is None
    assert pipeline.list_ghost_patches(git_repo) == ()


def test_test_already_failing_at_head_is_pre_existing_and_produces_no_patch(git_repo: Path):
    """Acceptance #4: a test that already failed at HEAD, before this
    edit, is `pre-existing` and must not be escalated into a patch."""
    _setup_simple_repo(git_repo, test_content=_TEST_APP_PRE_EXISTING)
    config = _config(git_repo)

    before_edit = "def add(a, b):\n    return a + b\n\n\n# trivial\n"
    after_fix = "def add(a, b):\n    return a + b + 994\n\n\n# trivial\n"
    worktree, changeset = _edit_and_sync(git_repo, "app.py", before_edit)
    fix = _diff("app.py", before_edit, after_fix)
    test = "from app import add\n\n\ndef test_shadowci_regression():\n    assert add(2, 3) == 999\n"

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[test], fixes=[fix])
    )

    assert outcome.ghost_patch is None
    assert pipeline.list_ghost_patches(git_repo) == ()


def test_rejecting_one_classs_patch_leaves_a_different_classs_score_untouched(
    git_repo: Path, tmp_path: Path
):
    """Acceptance #5: two failures that resolve to the exact same checks
    (`pytest-targeted` in both cases below) must still get different
    fingerprints once they're classified differently, so recording a
    reject for one class's history never moves the other's score."""
    config = _config(git_repo)
    _setup_simple_repo(git_repo)
    worktree, changeset = _edit_and_sync(git_repo, "tests/test_app.py", _TEST_APP_EDITED)
    fix = _diff("app.py", _APP_ORIGINAL, "def add(a, b):\n    return a + b + 5\n")
    test = "from app import add\n\n\ndef test_shadowci_regression():\n    assert add(2, 3) == 10\n"
    test_edited_outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: ScriptedProvider(tests=[test], fixes=[fix])
    )
    assert test_edited_outcome.decision is not None
    test_edited_fp = test_edited_outcome.decision.fingerprint
    assert test_edited_fp.startswith("test-edited:")

    repo2 = _make_repo(tmp_path / "repo2")
    config2 = _config(repo2)
    _setup_simple_repo(repo2)
    worktree2, changeset2 = _edit_and_sync(repo2, "app.py", _APP_BROKEN)
    fix2 = _diff("app.py", _APP_BROKEN, _APP_ORIGINAL)
    unclear_outcome = pipeline.run_checks_and_maybe_escalate(
        changeset2, worktree2, config2, lambda name: ScriptedProvider(tests=[_TEST_APP], fixes=[fix2])
    )
    assert unclear_outcome.decision is not None
    unclear_fp = unclear_outcome.decision.fingerprint
    assert unclear_fp.startswith("unclear-behavior-change:")

    # The two classes must not collapse into the same fingerprint just
    # because both resolved to the same underlying check name.
    assert test_edited_fp != unclear_fp

    store = SignalStore(tmp_path / "shared-signals.json")
    store.record(test_edited_fp, Signal.ACCEPT, "a1")
    store.record(test_edited_fp, Signal.ACCEPT, "a2")
    policy = InterruptionPolicy(store, threshold=0.34, min_signals=2)
    before = policy.evaluate_fingerprint(test_edited_fp)
    assert before.interrupt is True

    store.record(unclear_fp, Signal.REJECT, "r1")
    store.record(unclear_fp, Signal.REJECT, "r2")

    after = policy.evaluate_fingerprint(test_edited_fp)
    assert after == before


def test_legacy_plain_fingerprint_signals_still_load_without_crashing(git_repo: Path, capsys):
    """Acceptance #6: a signals file written by the previous version (a
    plain `pytest-targeted` fingerprint, no class prefix) must keep
    loading and showing up in `shadowci signals` without crashing,
    alongside a new, class-prefixed fingerprint."""
    signals_path = git_repo / ".shadowci" / "signals.json"
    signals_path.parent.mkdir(parents=True, exist_ok=True)
    signals_path.write_text(
        json.dumps(
            [
                {
                    "fingerprint": "pytest-targeted",
                    "signal": "reject",
                    "patch_digest": "legacy1",
                    "recorded_at": 1700000000.0,
                },
                {
                    "fingerprint": "unclear-behavior-change:pytest-targeted",
                    "signal": "accept",
                    "patch_digest": "new1",
                    "recorded_at": 1700000100.0,
                },
            ]
        )
    )

    exit_code = main(["signals", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "pytest-targeted: 0 accept(s), 1 reject(s), 0 ignore(s) -> score=0.00, stays silent" in out
    assert (
        "unclear-behavior-change:pytest-targeted: 1 accept(s), 0 reject(s), 0 ignore(s) "
        "-> score=0.00, stays silent"
        in out
    )
