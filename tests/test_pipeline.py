import subprocess
from pathlib import Path

from shadowci import pipeline
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers import create_provider as real_create_provider
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree

_FIX = (
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1,2 +1,2 @@\n"
    " def add(a, b):\n"
    "-    return a + b + 1\n"
    "+    return a + b\n"
)
_TEST = "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _commit_tests_dir(git_repo: Path) -> None:
    """The shadow worktree checks out whatever was committed at creation
    time — a test file must be committed before `sync()` first creates it,
    same as `shadowci watch` sees in practice (tests are already in HEAD
    before the buggy edit lands)."""
    subprocess.run(["git", "add", "tests"], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add tests"], cwd=git_repo, check=True, capture_output=True
    )


def _sync_changeset(git_repo: Path, content: str) -> tuple[ShadowWorktree, ChangeSet]:
    live_file = git_repo / "app.py"
    live_file.write_text(content)
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(_config(git_repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def test_no_escalation_when_checks_pass(git_repo: Path):
    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, real_create_provider
    )

    assert outcome.failing_checks == ()
    assert outcome.ghost_patch is None
    assert outcome.decision is None


def test_unknown_provider_is_reported_without_raising(git_repo: Path):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text("def test_app():\n    assert False\n")
    _commit_tests_dir(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)
    config.provider = "not-a-real-provider"

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, real_create_provider
    )

    assert outcome.failing_checks
    assert outcome.unknown_provider_error is not None
    assert "unknown provider" in outcome.unknown_provider_error
    assert outcome.ghost_patch is None


def test_provider_runtime_error_is_captured(git_repo: Path):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text("def test_app():\n    assert False\n")
    _commit_tests_dir(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    class _Exploding(ScriptedProvider):
        def write_regression_test(self, changeset, check_results):
            raise RuntimeError("boom")

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset, worktree, config, lambda name: _Exploding()
    )

    assert outcome.provider_name == "scripted"
    assert outcome.provider_error == "boom"
    assert outcome.ghost_patch is None


def test_ghost_patch_build_failure_is_captured(git_repo: Path):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_app():\n    assert add(2, 3) == 5\n"
    )
    _commit_tests_dir(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_TEST], fixes=["not a real diff"]),
    )

    assert outcome.provider_name == "scripted"
    assert outcome.ghost_patch_error is not None
    assert "did not apply cleanly" in outcome.ghost_patch_error
    assert outcome.ghost_patch is None


def test_verified_fix_is_saved_as_a_ghost_patch_with_metadata(git_repo: Path):
    (git_repo / "tests").mkdir()
    (git_repo / "tests" / "test_app.py").write_text(_TEST)
    _commit_tests_dir(git_repo)

    worktree, changeset = _sync_changeset(git_repo, "def add(a, b):\n    return a + b + 1\n")
    config = _config(git_repo)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_TEST], fixes=[_FIX]),
    )

    assert outcome.ghost_patch is not None
    assert outcome.ghost_patch_path is not None
    assert outcome.ghost_patch_path.exists()
    assert outcome.decision is not None
    assert outcome.decision.interrupt is False  # no signal history yet -> silent

    metadata = pipeline.load_patch_metadata(outcome.ghost_patch_path)
    assert metadata is not None
    assert metadata["fingerprint"] == outcome.decision.fingerprint
    assert metadata["summary"] == outcome.ghost_patch.evidence.summary


def test_accept_patch_applies_and_records_signal(git_repo: Path):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a + b\n+    return a + b  # fixed\n"
    )
    patch_file.with_suffix(".json").write_text(
        '{"digest": "abc123", "fingerprint": "pytest", "summary": "a fix"}'
    )

    result = pipeline.accept_patch(git_repo, patch_file)

    assert result.ok
    assert result.fingerprint == "pytest"
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b  # fixed\n"

    store = pipeline.signal_store(git_repo)
    assert len(store.for_fingerprint("pytest")) == 1


def test_accept_patch_reports_a_missing_file(git_repo: Path):
    result = pipeline.accept_patch(git_repo, git_repo / "nope.patch")
    assert not result.ok
    assert "no such patch file" in result.message


def test_reject_patch_discards_and_records_signal(git_repo: Path):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text("irrelevant")
    patch_file.with_suffix(".json").write_text(
        '{"digest": "abc123", "fingerprint": "pytest", "summary": "a fix"}'
    )

    result = pipeline.reject_patch(git_repo, patch_file)

    assert result.ok
    assert not patch_file.exists()
    assert not patch_file.with_suffix(".json").exists()
    store = pipeline.signal_store(git_repo)
    assert len(store.for_fingerprint("pytest")) == 1


def test_ignore_patch_leaves_file_and_records_signal(git_repo: Path):
    patch_file = git_repo / "fix.patch"
    patch_file.write_text("irrelevant")
    patch_file.with_suffix(".json").write_text(
        '{"digest": "abc123", "fingerprint": "pytest", "summary": "a fix"}'
    )

    result = pipeline.ignore_patch(git_repo, patch_file)

    assert result.ok
    assert patch_file.exists()
    store = pipeline.signal_store(git_repo)
    assert len(store.for_fingerprint("pytest")) == 1
