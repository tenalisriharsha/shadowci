"""Pipeline-level coverage for issue #40's "don't judge this yet" paths.

`shadowci pause` and a `# shadowci: wip` marker on a touched file must
hold back the model call entirely while deterministic checks keep
running and still sync into the shadow worktree (acceptance test 3).
Neither path had any test coverage before this file. `_never_call_provider`
stands in for "a provider that fails the test if called": it is a
`create_provider` factory, so if escalation ever reaches the point of
constructing a provider while paused or marked wip, the test fails
immediately instead of silently calling a real provider.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from shadowci import pause as pause_module
from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.pause import PAUSE_MESSAGE
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree

_TEST = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 1\n"
    "+    return a + b\n"
)


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _commit_tests_dir(git_repo: Path) -> None:
    subprocess.run(["git", "add", "tests"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tests"], cwd=git_repo, check=True, capture_output=True
    )


def _write_failing_test(git_repo: Path) -> None:
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text(_TEST)
    _commit_tests_dir(git_repo)


def _sync_changeset(git_repo: Path, content: str, filename: str = "app.py"):
    live_file = git_repo / filename
    live_file.write_text(content)
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(_config(git_repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def _never_call_provider(name: str):
    raise AssertionError("create_provider must not be called while escalation is held back")


def test_paused_holds_back_escalation_without_constructing_a_provider(git_repo: Path):
    _write_failing_test(git_repo)
    pause_module.pause(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, _never_call_provider
    )

    assert outcome.failing_checks
    assert outcome.paused_message == PAUSE_MESSAGE
    assert outcome.ghost_patch is None
    assert outcome.provider_name is None


def test_paused_still_runs_checks_and_syncs_into_the_shadow_worktree(git_repo: Path):
    _write_failing_test(git_repo)
    pause_module.pause(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, _never_call_provider
    )

    assert outcome.check_results
    assert any(not result.passed for result in outcome.check_results)
    assert (worktree.path / "app.py").read_text() == "def add(a, b):\n    return a + b + 1\n"


def test_wip_marker_on_the_touched_file_holds_back_escalation(git_repo: Path):
    _write_failing_test(git_repo)

    content = "# shadowci: wip\ndef add(a, b):\n    return a + b + 1\n"
    worktree, changeset = _sync_changeset(git_repo, content)
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, _never_call_provider
    )

    assert outcome.failing_checks
    assert outcome.paused_message == PAUSE_MESSAGE
    assert outcome.ghost_patch is None
    assert outcome.provider_name is None


def test_wip_marker_on_an_untouched_file_does_not_hold_back_escalation(git_repo: Path):
    """The marker holds back escalation "for that file only" (issue #40) — a
    marker living in some other file the change set never touched must not
    affect this change set at all, so escalation proceeds as normal."""
    _write_failing_test(git_repo)
    (git_repo / "other.py").write_text("# shadowci: wip\nX = 1\n")

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_TEST], fixes=[_FIX]),
    )

    assert outcome.paused_message is None
    assert outcome.provider_name == "scripted"
    assert outcome.ghost_patch is not None
