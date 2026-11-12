"""Regression tests for issue #37: a fix that puts back the user's own
intentional edit must be saved as a question, not a silently-applied
"verified fix".

Today `EscalationPolicy`/`providers.prompts` only ever ask a provider to
"propose a minimal fix" — when the user changed behavior on purpose and an
old test now fails (an assertion failure, not a crash), the model's
"natural" fix typically undoes exactly that edit, and
`run_checks_and_maybe_escalate` saves it as an ordinary verified ghost
patch. The pipeline has everything it needs to catch this deterministically
(the shadow worktree's `git diff HEAD` *is* the user's own edit, taken
before any fix is applied) but never looks at it.

These tests pin the deterministic fix described in the issue:

- a fix that reverts lines from the user's own uncommitted edit, for an
  *assertion*-type failure, must never be saved with `kind: "fix"` —
  instead two independently-verified options ("keep old behaviour" /
  "update the test") are saved under one shared `group`;
- a *crash*-type failure (issue #36) stays exempt and is still a normal,
  auto-saved `kind: "fix"` patch, even though its diff also restores the
  user's removed/changed lines;
- a genuine regression whose correct fix lives in a file the user didn't
  touch is also a normal fix — nothing to ask about, there's no revert;
- applying one of the two question options retires its sibling and records
  exactly one signal, never two;
- a ghost patch saved before this field existed (no `kind` key at all)
  keeps listing and applying exactly as before.
"""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from shadowci import pipeline
from shadowci.cli import main
from shadowci.config import ShadowCIConfig
from shadowci.evaluation.scenarios import (
    garbage_code_scenario,
    intentional_behavior_change_scenario,
)
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _commit_all(repo: Path, message: str) -> None:
    _run_git(["add", "-A"], cwd=repo)
    _run_git(["commit", "-q", "-m", message], cwd=repo)


def _write(repo: Path, relative: str, content: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _sync(config: ShadowCIConfig, path: Path) -> tuple[ShadowWorktree, ChangeSet]:
    changeset = ChangeSet(events=(FileChangeEvent(path=path, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(config)
    worktree.sync(list(changeset.events))
    return worktree, changeset


def _make_diff(relpath: str, before: str, after: str) -> str:
    diff_lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{relpath}",
        tofile=f"b/{relpath}",
    )
    text = "".join(diff_lines)
    return text if text.endswith("\n") else text + "\n"


def _diff_target_files(diff_text: str) -> set[str]:
    return set(re.findall(r"^\+\+\+ b/(.+)$", diff_text, re.MULTILINE))


def _question_listings(root: Path) -> list:
    return [
        listing
        for listing in pipeline.list_ghost_patches(root)
        if (listing.metadata or {}).get("kind") == "question"
    ]


# ---------------------------------------------------------------------------
# The intentional behavior change scenario from issue #35: average([])
# returns 0 on purpose instead of raising, breaking the old test.
# ---------------------------------------------------------------------------


def _intentional_change_pieces():
    scenario = intentional_behavior_change_scenario()
    edit = scenario.session.edits[0]
    module_relpath = edit.relative_path
    clean_content = scenario.session.initial_files[module_relpath]
    changed_content = edit.content
    test_relpath = next(
        path for path in scenario.session.initial_files if path != module_relpath
    )
    assert scenario.regression_test is not None
    assert scenario.proposed_fix is not None
    assert changed_content is not None
    return (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        scenario.regression_test,
        scenario.proposed_fix,
    )


def _run_intentional_change_pipeline(git_repo: Path):
    (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        regression_test,
        proposed_fix,
    ) = _intentional_change_pieces()

    _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add average() and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, module_relpath, changed_content)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[proposed_fix]),
    )
    return outcome, config, clean_content, changed_content


def test_intentional_behavior_change_saves_two_question_patches_not_a_fix(git_repo: Path):
    _outcome, config, _clean, _changed = _run_intentional_change_pipeline(git_repo)

    listings = pipeline.list_ghost_patches(config.project_root)
    kinds = [(listing.metadata or {}).get("kind") for listing in listings]

    assert "fix" not in kinds, f"expected no kind='fix' patch, got kinds={kinds}"
    assert kinds.count("question") == 2, f"expected exactly two questions, got kinds={kinds}"

    groups = {
        (listing.metadata or {}).get("group")
        for listing in listings
        if (listing.metadata or {}).get("kind") == "question"
    }
    assert len(groups) == 1, "the two question options must share one group id"
    assert None not in groups


def test_intentional_behavior_change_options_are_revert_and_update_test(git_repo: Path):
    _outcome, config, _clean, _changed = _run_intentional_change_pipeline(git_repo)

    questions = _question_listings(config.project_root)
    assert len(questions) == 2

    targets = {listing.patch_path: _diff_target_files(listing.patch_path.read_text()) for listing in questions}

    revert = next(
        (
            listing
            for listing in questions
            if "average.py" in targets[listing.patch_path]
            and "tests/test_average.py" not in targets[listing.patch_path]
        ),
        None,
    )
    update_test = next(
        (
            listing
            for listing in questions
            if "tests/test_average.py" in targets[listing.patch_path]
        ),
        None,
    )

    assert revert is not None, f"expected one option reverting average.py, got {targets}"
    assert update_test is not None, f"expected one option updating the test, got {targets}"
    assert revert is not update_test


def test_applying_the_revert_option_restores_old_behavior_and_retires_sibling(git_repo: Path):
    _outcome, config, clean_content, _changed = _run_intentional_change_pipeline(git_repo)
    root = config.project_root

    questions = _question_listings(root)
    assert len(questions) == 2
    revert = next(
        listing
        for listing in questions
        if "average.py" in _diff_target_files(listing.patch_path.read_text())
        and "tests/test_average.py" not in _diff_target_files(listing.patch_path.read_text())
    )
    sibling = next(listing for listing in questions if listing is not revert)

    result = pipeline.accept_patch(root, revert.patch_path)

    assert result.ok, result.message
    assert (root / "average.py").read_text() == clean_content

    # the sibling option is retired, with no signal recorded for it
    assert not sibling.patch_path.exists()
    assert not pipeline.metadata_path(sibling.patch_path).exists()

    store = pipeline.signal_store(root)
    assert len(store.all()) == 1  # exactly one accept — not one per option


def test_applying_the_update_test_option_keeps_new_behavior_and_passes_the_suite(
    git_repo: Path,
):
    _outcome, config, _clean, changed_content = _run_intentional_change_pipeline(git_repo)
    root = config.project_root

    questions = _question_listings(root)
    assert len(questions) == 2
    update_test = next(
        listing
        for listing in questions
        if "tests/test_average.py" in _diff_target_files(listing.patch_path.read_text())
    )
    sibling = next(listing for listing in questions if listing is not update_test)

    result = pipeline.accept_patch(root, update_test.patch_path)

    assert result.ok, result.message
    assert (root / "average.py").read_text() == changed_content  # the user's edit stands

    assert not sibling.patch_path.exists()
    assert not pipeline.metadata_path(sibling.patch_path).exists()

    suite = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_average.py"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert suite.returncode == 0, suite.stdout + suite.stderr

    store = pipeline.signal_store(root)
    assert len(store.all()) == 1


def test_provider_intentional_reply_becomes_a_question_not_a_crash(git_repo: Path):
    """Issue #39: a provider may answer a fix request with the literal line
    `INTENTIONAL: <reason>` instead of a diff when the failure looks like a
    deliberate behavior change. shadowci must treat that reply the same way
    issue #37 treats a fix that reverts the user's edit — two verified
    options saved as a question — rather than trying to `git apply` the
    reply as a patch and failing."""
    (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        regression_test,
        _original_proposed_fix,
    ) = _intentional_change_pieces()

    _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add average() and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, module_relpath, changed_content)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(
            tests=[regression_test],
            fixes=["INTENTIONAL: average([]) now returns 0 on purpose"],
        ),
    )

    assert outcome.ghost_patch_error is None, outcome.ghost_patch_error
    assert outcome.ghost_patch is None

    listings = pipeline.list_ghost_patches(config.project_root)
    kinds = [(listing.metadata or {}).get("kind") for listing in listings]
    assert "fix" not in kinds, f"expected no kind='fix' patch, got kinds={kinds}"
    assert kinds.count("question") == 2, f"expected exactly two questions, got kinds={kinds}"


def test_watch_asks_instead_of_saving_a_fix_for_an_intentional_change(
    git_repo: Path, capsys
):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )
    (
        module_relpath,
        test_relpath,
        clean_content,
        changed_content,
        regression_test,
        proposed_fix,
    ) = _intentional_change_pieces()
    _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add average() and its test")

    result: dict[str, int] = {}

    def run_watch() -> None:
        import shadowci.cli as cli_module

        original_create_provider = cli_module.create_provider
        cli_module.create_provider = lambda name: ScriptedProvider(
            tests=[regression_test], fixes=[proposed_fix]
        )
        try:
            result["code"] = main(
                ["watch", str(git_repo), "--max-cycles", "30", "--interval", "0.02"]
            )
        finally:
            cli_module.create_provider = original_create_provider

    thread = threading.Thread(target=run_watch)
    thread.start()
    time.sleep(0.05)
    (git_repo / module_relpath).write_text(changed_content)
    thread.join(timeout=10)

    out = capsys.readouterr().out.lower()

    assert result.get("code") == 0
    assert "update the test" in out
    assert "keep the old behav" in out

    ghost_dir = git_repo / ".shadowci" / "ghost"
    metadata_files = list(ghost_dir.glob("*.json"))
    assert len(metadata_files) == 2
    kinds = [json.loads(path.read_text()).get("kind") for path in metadata_files]
    assert kinds.count("question") == 2

    # both digests the user can act on are named in the watch output
    digests = [path.stem for path in metadata_files]
    for digest in digests:
        assert digest in out


# ---------------------------------------------------------------------------
# Crash-type failures (issue #36) are exempt: still a normal, auto-saved fix.
# ---------------------------------------------------------------------------


def test_crash_type_failure_fix_is_still_a_normal_verified_fix(git_repo: Path):
    scenario = garbage_code_scenario()
    edit = scenario.session.edits[0]
    module_relpath = edit.relative_path
    clean_content = scenario.session.initial_files[module_relpath]
    buggy_content = edit.content
    test_relpath = next(
        path for path in scenario.session.initial_files if path != module_relpath
    )
    regression_test = scenario.regression_test
    proposed_fix = scenario.proposed_fix
    assert buggy_content is not None
    assert regression_test is not None
    assert proposed_fix is not None

    _write(git_repo, module_relpath, clean_content)
    _write(git_repo, test_relpath, regression_test)
    _commit_all(git_repo, "add calc.add and its test")

    config = _config(git_repo)
    module_path = _write(git_repo, module_relpath, buggy_content)
    worktree, changeset = _sync(config, module_path)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[regression_test], fixes=[proposed_fix]),
    )

    assert outcome.ghost_patch is not None, outcome.ghost_patch_error
    assert outcome.ghost_patch_path is not None

    metadata = pipeline.load_patch_metadata(outcome.ghost_patch_path)
    assert metadata is not None
    assert metadata.get("kind", "fix") == "fix"
    assert _question_listings(config.project_root) == []


# ---------------------------------------------------------------------------
# A genuine regression whose correct fix lives in a different file than the
# one the user edited: nothing of the user's own lines is being put back, so
# there is nothing to ask about either.
# ---------------------------------------------------------------------------


def test_genuine_regression_in_a_different_file_is_a_normal_fix_no_question(git_repo: Path):
    helper_before = "def helper():\n    return 1\n"
    other_py = "from helper import helper\n\n\ndef compute():\n    return helper() + 10\n"
    test_other = (
        "from other import compute\n\n\ndef test_compute():\n    assert compute() == 11\n"
    )

    _write(git_repo, "helper.py", helper_before)
    _write(git_repo, "other.py", other_py)
    _write(git_repo, "tests/test_other.py", test_other)
    _commit_all(git_repo, "add helper, other and its test")

    config = _config(git_repo)
    helper_after = "def helper():\n    return 2\n"
    helper_path = _write(git_repo, "helper.py", helper_after)
    worktree, changeset = _sync(config, helper_path)

    other_py_fixed = "from helper import helper\n\n\ndef compute():\n    return helper() + 9\n"
    proposed_fix = _make_diff("other.py", other_py, other_py_fixed)

    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[test_other], fixes=[proposed_fix]),
    )

    assert outcome.ghost_patch is not None, outcome.ghost_patch_error
    assert outcome.ghost_patch_path is not None

    metadata = pipeline.load_patch_metadata(outcome.ghost_patch_path)
    assert metadata is not None
    assert metadata.get("kind", "fix") == "fix"
    assert _question_listings(config.project_root) == []

    # the user's own edit to helper.py is untouched by the saved fix
    assert "helper.py" not in _diff_target_files(outcome.ghost_patch.diff)


# ---------------------------------------------------------------------------
# Backward compatibility: a ghost patch saved before `kind`/`group` existed.
# ---------------------------------------------------------------------------


def test_ghost_patch_without_a_kind_field_still_lists_and_applies(git_repo: Path, capsys):
    patch_file = git_repo / ".shadowci" / "ghost" / "oldstyle01.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a + b\n+    return a + b  # fixed\n"
    )
    patch_file.with_suffix(".json").write_text(
        json.dumps(
            {
                "digest": "oldstyle01",
                "fingerprint": "pytest",
                "summary": "an old-style fix with no kind/group field",
                "files": ["app.py"],
            }
        )
    )

    listings = pipeline.list_ghost_patches(git_repo)
    assert len(listings) == 1
    assert "kind" not in listings[0].metadata
    assert "group" not in listings[0].metadata

    exit_code = main(["ghosts", str(git_repo)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "oldstyle01" in out

    exit_code = main(["apply-ghost", str(patch_file), str(git_repo)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b  # fixed\n"

    store = pipeline.signal_store(git_repo)
    assert len(store.all()) == 1
