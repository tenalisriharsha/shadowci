"""Regression tests for issue #36: a crash-type scenario the planned revert
check (issue #37) must never mistake for an over-eager revert.

A fix for genuinely broken code (code that can't even run) often looks,
diff-wise, exactly like reverting the user's last change — because
restoring the last-good lines from HEAD *is* the correct fix. These
scenarios pin that behavior down: the pipeline must still produce a
confident, verified, auto-accepted fix for a crash, and the `reverted_user_change`
metric (added in issue #35) must not flag it, even though its diff
literally puts the user's removed/changed lines back.
"""

from __future__ import annotations

import ast

import pytest

from shadowci.evaluation.harness import run_scenario, run_suite
from shadowci.evaluation.scenarios import (
    built_in_scenarios,
    garbage_code_import_error_scenario,
    garbage_code_scenario,
)


def _module_contents(scenario):
    """The module's committed (clean) and edited (buggy) source as strings."""
    edit = scenario.session.edits[0]
    clean = scenario.session.initial_files[edit.relative_path]
    buggy = edit.content
    assert buggy is not None
    return clean, buggy


def test_garbage_code_scenario_is_a_crash_type_bug_scenario():
    scenario = garbage_code_scenario()

    assert scenario.bug_expected is True
    assert scenario.expected_outcome == "fix"
    assert scenario.regression_test
    assert scenario.proposed_fix
    assert len(scenario.session.edits) == 1


def test_garbage_code_import_error_scenario_is_a_crash_type_bug_scenario():
    scenario = garbage_code_import_error_scenario()

    assert scenario.bug_expected is True
    assert scenario.expected_outcome == "fix"
    assert scenario.regression_test
    assert scenario.proposed_fix
    assert len(scenario.session.edits) == 1


def test_garbage_code_scenario_edit_crashes_on_import_but_parses_as_valid_python():
    # The edit must NOT be an actual SyntaxError: shadowci's ChangeSetDetector
    # treats a file that fails to parse as "still mid-flight" and never even
    # promotes it to a change set, so a true syntax error could never reach
    # the pipeline at all. The crash has to surface at runtime (NameError),
    # the way a stray identifier or a misspelled name actually behaves.
    scenario = garbage_code_scenario()
    clean, buggy = _module_contents(scenario)

    ast.parse(buggy)  # must not raise SyntaxError

    exec(compile(clean, "<clean>", "exec"), {})  # the committed version runs fine

    with pytest.raises(NameError):
        exec(compile(buggy, "<buggy>", "exec"), {})


def test_garbage_code_import_error_scenario_edit_crashes_with_an_import_error():
    scenario = garbage_code_import_error_scenario()
    clean, buggy = _module_contents(scenario)

    ast.parse(buggy)  # must not raise SyntaxError

    exec(compile(clean, "<clean>", "exec"), {})  # the committed version runs fine

    with pytest.raises(ImportError):
        exec(compile(buggy, "<buggy>", "exec"), {})


def test_garbage_code_scenarios_are_part_of_the_built_in_suite():
    scenarios = built_in_scenarios()

    assert garbage_code_scenario() in scenarios
    assert garbage_code_import_error_scenario() in scenarios


def test_run_scenario_detects_and_verifies_a_fix_for_a_syntactically_broken_edit():
    result = run_scenario(garbage_code_scenario())

    assert result.detected is True
    assert result.true_positive is True
    assert result.action == "accept"
    verified = [cs for cs in result.changesets if cs.outcome.ghost_patch is not None]
    assert len(verified) == 1
    assert verified[0].outcome.ghost_patch.evidence.all_checks_passed


def test_run_scenario_detects_and_verifies_a_fix_for_a_misspelled_import():
    result = run_scenario(garbage_code_import_error_scenario())

    assert result.detected is True
    assert result.true_positive is True
    assert result.action == "accept"
    verified = [cs for cs in result.changesets if cs.outcome.ghost_patch is not None]
    assert len(verified) == 1
    assert verified[0].outcome.ghost_patch.evidence.all_checks_passed


def test_run_suite_never_flags_a_crash_fix_as_a_reverted_user_change():
    # Both fixes restore the committed lines over the user's broken edit —
    # by the raw diff that would look exactly like reverting a real
    # behavior change (issue #35's false positive). It must not be counted
    # as one here: the code could not run, so restoring it was simply correct.
    report = run_suite((garbage_code_scenario(), garbage_code_import_error_scenario()))

    assert report.true_positives == 2
    assert report.false_negatives == 0
    assert report.reverted_user_change == 0
