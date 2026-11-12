"""Regression tests for issue #53's evaluation-harness coverage: nothing in
the recorded-session format or the harness could ever reproduce "the shadow
worktree's HEAD goes stale" at all, because a `RecordedSession` has no way
to express "the user committed here" — only file edits.

The fix extends `RecordedSession` with a `commits` field: offsets (seconds
from the start of the session, same units as `RecordedEdit.offset_seconds`)
at which the harness commits whatever is on disk in the sandboxed live
repo. A built-in scenario uses it to put a commit strictly between two
edits, and the suite report gets a new `reverted_committed_work` count —
the harness-level analogue of `revert_check.diff_head_reversed` wrongly
reverting already-committed work (the core bug in this issue) — which must
be 0 for a correctly-fixed pipeline.

Backward compatibility matters here specifically because every scenario
JSON file saved before this field existed has no `"commits"` key at all;
`session_from_dict` must still load those unchanged.
"""

from __future__ import annotations

from shadowci.evaluation.harness import run_scenario, run_suite
from shadowci.evaluation.scenarios import built_in_scenarios
from shadowci.evaluation.session import (
    RecordedEdit,
    RecordedSession,
    session_from_dict,
    session_to_dict,
)
from shadowci.events import ChangeKind


def test_recorded_session_accepts_a_commits_field():
    session = RecordedSession(
        name="s",
        initial_files={"a.py": "x = 1\n"},
        edits=(
            RecordedEdit("a.py", ChangeKind.MODIFIED, 0.0, "x = 2\n"),
            RecordedEdit("a.py", ChangeKind.MODIFIED, 1.0, "x = 3\n"),
        ),
        commits=(0.5,),
    )
    assert session.commits == (0.5,)


def test_recorded_session_defaults_to_no_commits():
    session = RecordedSession(name="s")
    assert session.commits == ()


def test_session_round_trip_through_dict_preserves_commits():
    session = RecordedSession(
        name="s",
        initial_files={"a.py": "x = 1\n"},
        edits=(RecordedEdit("a.py", ChangeKind.MODIFIED, 2.0, "x = 2\n"),),
        commits=(1.0, 2.5),
    )
    loaded = session_from_dict(session_to_dict(session))
    assert loaded.commits == (1.0, 2.5)
    assert loaded == session


def test_loading_a_session_dict_without_a_commits_key_still_works():
    # exactly what a scenario JSON file saved before this field existed
    # looks like: no "commits" key present at all.
    data = {
        "name": "pre-existing-session",
        "description": "",
        "initial_files": {"a.py": "x = 1\n"},
        "edits": [
            {
                "relative_path": "a.py",
                "kind": "modified",
                "offset_seconds": 0.0,
                "content": "x = 2\n",
            }
        ],
    }
    loaded = session_from_dict(data)
    assert loaded.commits == ()
    assert loaded.name == "pre-existing-session"


def test_a_built_in_scenario_has_a_commit_strictly_between_two_edits():
    scenarios = built_in_scenarios()
    with_commits = [scenario for scenario in scenarios if scenario.session.commits]

    assert with_commits, (
        "expected at least one built-in scenario whose session has a commit "
        "between two edits (issue #53's reproduction: commit a feature, then "
        "make a small uncommitted edit)"
    )

    scenario = with_commits[0]
    edits = sorted(scenario.session.edits, key=lambda edit: edit.offset_seconds)
    assert len(edits) >= 2, "need at least two edits for a commit to land between"

    offsets = [edit.offset_seconds for edit in edits]
    assert any(
        offsets[i] < commit_offset < offsets[i + 1]
        for commit_offset in scenario.session.commits
        for i in range(len(offsets) - 1)
    ), (
        f"the commit offset(s) {scenario.session.commits} must land strictly "
        f"between two consecutive edit offsets {offsets}"
    )


def test_running_the_commit_scenario_reports_no_reverted_committed_work():
    scenarios = built_in_scenarios()
    scenario = next(s for s in scenarios if s.session.commits)

    result = run_scenario(scenario)

    assert result.reverted_committed_work is False, (
        "the fix committed between the two edits must survive the replay — "
        "this is issue #53's core bug, caught at the evaluation-harness level"
    )


def test_running_the_full_suite_reports_zero_reverted_committed_work():
    report = run_suite(built_in_scenarios())

    assert report.reverted_committed_work == 0
