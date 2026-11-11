"""Regression tests for issue #37: a fix that puts back the user's own edit
is saved as a verified fix instead of asking.

When the user changes behaviour on purpose and an old test fails because of
it, the model's "minimal fix" typically just undoes the user's edit. Today
`pipeline.run_checks_and_maybe_escalate` verifies that revert against the
*new* code exactly like any other fix and saves it as a single verified
ghost patch (`kind` isn't even a concept yet) — which is exactly how
shadowci ends up telling the user their intentional change was a bug.

The fix for #37 is a deterministic check, run inside the real pipeline
(no model judgement): compare the proposed fix against `git diff HEAD` in
the shadow worktree, taken before the fix is applied. If the fix puts back
lines the user's own edit added or changed, and the failing check is an
assertion failure (not a crash), the patch must not be saved as a plain
`kind: "fix"` ghost patch. Instead two *verified* options get saved under
one `group`: a revert ("keep old behaviour") and a test-expectation update
("update the test") — see `GhostPatchBuilder`/`pipeline.save_ghost_patch`.

Crash-type failures (`NameError`, `ImportError`, `SyntaxError`, ...) stay
exempt: the code never even ran, so restoring the committed lines is simply
correct, regardless of whether that restoration "looks like" a revert.

A genuine accidental regression — the user modifies an existing line in one
file, breaking a test for something else — must also stay a normal,
single, auto-saved `kind: "fix"` patch: the existing off-by-one-style
scenarios (issue #35/#36) already pin that a modification-style revert
fix keeps working exactly as before, so this file only adds a second,
differently-named example of that same shape (acceptance test 3 from the
issue) alongside the new addition-style "ask" case (acceptance test 1).
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from shadowci import pipeline
from shadowci.cli import main
from shadowci.config import ShadowCIConfig
from shadowci.events import ChangeKind, ChangeSet, FileChangeEvent
from shadowci.providers.scripted import ScriptedProvider
from shadowci.shadow_worktree import ShadowWorktree

GHOST_DIRNAME = Path(".shadowci") / "ghost"
SIGNALS_PATH = Path(".shadowci") / "signals.json"

# --- the intentional-change example from the issue: average([]) switches
# from raising to returning 0, on purpose, breaking the old test for it. ---

_AVERAGE_CLEAN = (
    "def average(values):\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)
_AVERAGE_CHANGED = (
    "def average(values):\n"
    "    if not values:\n"
    "        return 0\n"
    "    total = sum(values)\n"
    "    return total / len(values)\n"
)
_AVERAGE_TEST = (
    "import pytest\n\n"
    "from average import average\n\n\n"
    "def test_average_of_empty_list_raises():\n"
    "    with pytest.raises(ZeroDivisionError):\n"
    "        average([])\n"
)
_AVERAGE_REVERT_FIX = (
    "--- a/average.py\n+++ b/average.py\n@@ -1,5 +1,3 @@\n"
    " def average(values):\n"
    "-    if not values:\n"
    "-        return 0\n"
    "     total = sum(values)\n"
    "     return total / len(values)\n"
)

# --- a crash-type example (issue #36): a stray debug line crashes calc.py
# at import with a NameError. Restoring it is correct even though the fix's
# diff also "puts back" what the user added. ---

_CALC_CLEAN = "def add(a, b):\n    return a + b\n"
_CALC_CRASHED = "def add(a, b):\n    return a + b\ncache_result = cahce_result\n"
_CALC_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
_CALC_FIX = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,3 +1,2 @@\n"
    " def add(a, b):\n     return a + b\n-cache_result = cahce_result\n"
)

# --- a genuine accidental regression (acceptance test 3): the user
# modifies (not adds to) an existing line in helper.py, an unrelated test
# file (test_other.py) catches it. Same shape as the off-by-one scenarios
# that must keep working exactly as before. ---

_HELPER_CLEAN = "def helper(x):\n    return x * 2\n"
_HELPER_BROKEN = "def helper(x):\n    return x * 2 + 1\n"
_OTHER_TEST = "from helper import helper\n\n\ndef test_other():\n    assert helper(3) == 6\n"
_HELPER_FIX = (
    "--- a/helper.py\n+++ b/helper.py\n@@ -1,2 +1,2 @@\n"
    " def helper(x):\n"
    "-    return x * 2 + 1\n"
    "+    return x * 2\n"
)


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _config(repo: Path) -> ShadowCIConfig:
    return ShadowCIConfig(project_root=repo, shadow_dir=Path(".shadowci/shadow"))


def _commit(repo: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _run_git(["add", *files.keys()], cwd=repo)
    _run_git(["commit", "-q", "-m", "add files"], cwd=repo)


def _sync(repo: Path, relative_path: str, content: str) -> tuple[ShadowWorktree, ChangeSet]:
    live_file = repo / relative_path
    live_file.write_text(content)
    changeset = ChangeSet(events=(FileChangeEvent(path=live_file, kind=ChangeKind.MODIFIED),))
    worktree = ShadowWorktree(_config(repo))
    worktree.sync(list(changeset.events))
    return worktree, changeset


def test_genuine_bug_fix_elsewhere_is_still_saved_as_a_single_kind_fix_patch(git_repo: Path):
    # Acceptance test 3: a real regression in one file (helper.py), caught
    # by a test for something else (test_other.py) — the fix modifies an
    # existing line (same shape as the off-by-one scenarios), so it must
    # stay a normal, single, auto-saved fix: no question, no pair.
    _commit(git_repo, {"helper.py": _HELPER_CLEAN, "tests/test_other.py": _OTHER_TEST})
    config = _config(git_repo)

    worktree, changeset = _sync(git_repo, "helper.py", _HELPER_BROKEN)
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_OTHER_TEST], fixes=[_HELPER_FIX]),
    )

    assert outcome.ghost_patch is not None
    assert outcome.ghost_patch_path is not None

    metadata = pipeline.load_patch_metadata(outcome.ghost_patch_path)
    assert metadata is not None
    assert metadata["kind"] == "fix"

    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    assert len(list(ghost_dir.glob("*.patch"))) == 1


def test_crash_type_fix_is_still_saved_as_a_single_kind_fix_patch_even_though_it_reverts(
    git_repo: Path,
):
    # Acceptance test 2: the garbage-code scenario from issue #36. The fix
    # restores exactly what the user's buggy edit added — which, by raw
    # diff content, looks identical to the intentional-change case below —
    # but the crash (NameError) exempts it: still a single, auto-saved fix.
    _commit(git_repo, {"calc.py": _CALC_CLEAN, "tests/test_calc.py": _CALC_TEST})
    config = _config(git_repo)

    worktree, changeset = _sync(git_repo, "calc.py", _CALC_CRASHED)
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_CALC_TEST], fixes=[_CALC_FIX]),
    )

    assert outcome.ghost_patch is not None
    assert outcome.ghost_patch_path is not None

    metadata = pipeline.load_patch_metadata(outcome.ghost_patch_path)
    assert metadata is not None
    assert metadata["kind"] == "fix"

    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    assert len(list(ghost_dir.glob("*.patch"))) == 1


def _build_average_question(git_repo: Path) -> tuple[pipeline.PipelineOutcome, list[Path], list[dict]]:
    _commit(git_repo, {"average.py": _AVERAGE_CLEAN, "tests/test_average.py": _AVERAGE_TEST})
    config = _config(git_repo)

    worktree, changeset = _sync(git_repo, "average.py", _AVERAGE_CHANGED)
    outcome = pipeline.run_checks_and_maybe_escalate(
        changeset,
        worktree,
        config,
        lambda name: ScriptedProvider(tests=[_AVERAGE_TEST], fixes=[_AVERAGE_REVERT_FIX]),
    )

    ghost_dir = pipeline.ghost_patch_dir(git_repo)
    patch_paths = sorted(ghost_dir.glob("*.patch"))
    metadatas = [pipeline.load_patch_metadata(p) for p in patch_paths]
    return outcome, patch_paths, metadatas


def test_fix_that_reverts_an_intentional_change_is_saved_as_two_verified_questions(
    git_repo: Path,
):
    # Acceptance test 1: no patch with kind "fix" is saved; two options are
    # saved under one shared group instead.
    outcome, patch_paths, metadatas = _build_average_question(git_repo)

    assert outcome.ghost_patch is None

    assert len(patch_paths) == 2
    assert all(metadata is not None for metadata in metadatas)
    assert all(metadata["kind"] == "question" for metadata in metadatas)
    assert not any(metadata["kind"] == "fix" for metadata in metadatas)

    groups = {metadata["group"] for metadata in metadatas}
    assert len(groups) == 1
    assert None not in groups and "" not in groups


def _split_revert_and_test_update(
    patch_paths: list[Path],
) -> tuple[Path, Path]:
    """Tell apart the two saved average.py options by what their diff does.

    The "keep old behaviour" option's diff removes the guard clause the
    user added to average.py; the "update the test" option doesn't touch
    average.py's behaviour at all.
    """
    revert_path = None
    update_test_path = None
    for path in patch_paths:
        diff = path.read_text()
        if "-    if not values:" in diff:
            revert_path = path
        else:
            update_test_path = path
    assert revert_path is not None, "no option reverts average.py's new guard clause"
    assert update_test_path is not None, "no option leaves average.py's behaviour untouched"
    return revert_path, update_test_path


def test_applying_the_revert_option_keeps_old_behaviour_and_retires_its_sibling(
    git_repo: Path,
):
    # Acceptance test 4 (one direction): applying "keep old behaviour"
    # restores the pre-edit behaviour, discards the sibling option without
    # recording a signal for it, and records exactly one accept signal.
    _, patch_paths, _ = _build_average_question(git_repo)
    revert_path, update_test_path = _split_revert_and_test_update(patch_paths)

    result = pipeline.accept_patch(git_repo, revert_path)
    assert result.ok

    assert (git_repo / "average.py").read_text() == _AVERAGE_CLEAN
    assert (git_repo / "tests" / "test_average.py").read_text() == _AVERAGE_TEST

    assert not update_test_path.exists()
    assert not pipeline.metadata_path(update_test_path).exists()

    store = pipeline.signal_store(git_repo)
    assert len(store.all()) == 1


def test_applying_the_update_test_option_keeps_the_new_behaviour_and_retires_its_sibling(
    git_repo: Path,
):
    # Acceptance test 4 (the other direction): applying "update the test"
    # keeps the user's intentional change intact, changes the test's
    # expectation instead, discards the revert option without a signal for
    # it, and records exactly one accept signal. The resulting tree must
    # actually pass its own test suite.
    _, patch_paths, _ = _build_average_question(git_repo)
    revert_path, update_test_path = _split_revert_and_test_update(patch_paths)

    result = pipeline.accept_patch(git_repo, update_test_path)
    assert result.ok

    assert (git_repo / "average.py").read_text() == _AVERAGE_CHANGED
    updated_test = (git_repo / "tests" / "test_average.py").read_text()
    assert updated_test != _AVERAGE_TEST
    assert "pytest.raises(ZeroDivisionError)" not in updated_test

    assert not revert_path.exists()
    assert not pipeline.metadata_path(revert_path).exists()

    store = pipeline.signal_store(git_repo)
    assert len(store.all()) == 1

    completed = subprocess.run(
        ["python", "-m", "pytest", "tests/test_average.py"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_old_ghost_patch_metadata_without_kind_or_group_still_applies(git_repo: Path):
    # Acceptance test 5: a ghost patch saved before this issue's metadata
    # fields existed has no "kind"/"group" key at all — accept_patch must
    # not choke on that and must apply it as a normal fix, same as always.
    patch_file = git_repo / ".shadowci" / "ghost" / "legacy01.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a + b\n+    return a + b  # fixed\n"
    )
    patch_file.with_suffix(".json").write_text(
        json.dumps({"digest": "legacy01", "fingerprint": "pytest", "summary": "an old fix"})
    )

    result = pipeline.accept_patch(git_repo, patch_file)

    assert result.ok
    assert (git_repo / "app.py").read_text() == "def add(a, b):\n    return a + b  # fixed\n"
    store = pipeline.signal_store(git_repo)
    assert len(store.for_fingerprint("pytest")) == 1


def _line_mentioning(out: str, needle: str) -> str:
    for line in out.splitlines():
        if needle in line:
            return line
    raise AssertionError(f"no line in output mentions {needle!r}:\n{out}")


def test_cli_ghosts_lists_old_metadata_without_kind_normally(git_repo: Path, capsys):
    patch_file = git_repo / ".shadowci" / "ghost" / "legacy02.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(
        "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n-    return a + b\n+    return a + b  # fixed\n"
    )
    patch_file.with_suffix(".json").write_text(
        json.dumps(
            {
                "digest": "legacy02",
                "fingerprint": "pytest",
                "summary": "an old fix",
                "files": ["app.py"],
            }
        )
    )

    exit_code = main(["ghosts", str(git_repo)])
    out = capsys.readouterr().out

    assert exit_code == 0
    line = _line_mentioning(out, "legacy02")
    assert "an old fix" in line


def test_watch_asks_instead_of_silently_reverting_an_intentional_change(
    git_repo: Path, capsys
):
    (git_repo / "shadowci.toml").write_text(
        "[shadowci]\ndebounce_seconds = 0.02\nidle_seconds = 0.05\n"
    )
    _commit(git_repo, {"average.py": _AVERAGE_CLEAN, "tests/test_average.py": _AVERAGE_TEST})

    result: dict[str, int] = {}

    def run_watch() -> None:
        import shadowci.cli as cli_module

        original_create_provider = cli_module.create_provider
        cli_module.create_provider = lambda name: ScriptedProvider(
            tests=[_AVERAGE_TEST], fixes=[_AVERAGE_REVERT_FIX]
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
    (git_repo / "average.py").write_text(_AVERAGE_CHANGED)
    thread.join(timeout=10)

    out = capsys.readouterr().out

    assert result.get("code") == 0

    ghost_dir = git_repo / GHOST_DIRNAME
    patch_names = sorted(p.stem for p in ghost_dir.glob("*.patch"))
    assert len(patch_names) == 2

    lowered = out.lower()
    assert "update the test" in lowered
    assert "keep" in lowered and ("old behavior" in lowered or "old behaviour" in lowered)
    for digest in patch_names:
        assert digest in out

    assert "ghost patch verified" not in out
