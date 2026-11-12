from pathlib import Path

import pytest

from shadowci.evaluation.scenarios import (
    built_in_scenarios,
    intentional_behavior_change_scenario,
    load_scenario,
    off_by_one_operator_scenario,
    save_scenario,
    scenario_from_dict,
    scenario_to_dict,
)


def test_built_in_scenarios_have_unique_names_and_cover_both_classes():
    scenarios = built_in_scenarios()
    names = [scenario.session.name for scenario in scenarios]

    assert len(names) == len(set(names))
    assert any(scenario.bug_expected for scenario in scenarios)
    assert any(not scenario.bug_expected for scenario in scenarios)


def test_bug_scenarios_carry_a_regression_test_and_fix():
    # Issue #53 adds a bug scenario with a commit *between* two edits
    # (`committed_feature_then_bug_scenario`), so "exactly one edit" is no
    # longer true of every bug scenario — only "at least one" is.
    for scenario in built_in_scenarios():
        if scenario.bug_expected:
            assert scenario.regression_test
            assert scenario.proposed_fix
            assert len(scenario.session.edits) >= 1


def test_clean_scenarios_carry_no_test_or_fix():
    for scenario in built_in_scenarios():
        if not scenario.bug_expected:
            assert scenario.regression_test is None
            assert scenario.proposed_fix is None


def test_scenario_round_trips_through_a_dict():
    scenario = off_by_one_operator_scenario()

    restored = scenario_from_dict(scenario_to_dict(scenario))

    assert restored == scenario


def test_scenario_round_trips_through_a_file(tmp_path: Path):
    scenario = off_by_one_operator_scenario()
    path = tmp_path / "scenario.json"

    save_scenario(scenario, path)
    restored = load_scenario(path)

    assert restored == scenario


def test_built_in_scenarios_carry_an_expected_outcome_matching_bug_expected():
    for scenario in built_in_scenarios():
        if scenario.bug_expected:
            assert scenario.expected_outcome == "fix"
        else:
            assert scenario.expected_outcome == "silent"


def test_scenario_without_a_saved_expected_outcome_field_still_loads_and_behaves_as_before():
    # Simulates a scenario JSON file saved before `expected_outcome` existed.
    scenario = off_by_one_operator_scenario()
    data = scenario_to_dict(scenario)
    data.pop("expected_outcome", None)

    restored = scenario_from_dict(data)

    assert restored.bug_expected == scenario.bug_expected
    assert restored.regression_test == scenario.regression_test
    assert restored.proposed_fix == scenario.proposed_fix
    assert restored.expected_outcome == "fix"


def test_intentional_behavior_change_scenario_has_an_ask_outcome_not_fix_or_silent():
    scenario = intentional_behavior_change_scenario()

    assert scenario.expected_outcome == "ask"
    assert scenario.regression_test is not None
    assert scenario.proposed_fix is not None
    assert len(scenario.session.edits) == 1


def test_intentional_behavior_change_scenario_edit_changes_behavior_on_purpose():
    # average([]) used to raise; the edit makes it return 0 instead — an
    # intentional change, not an accidental off-by-one or inverted guard.
    scenario = intentional_behavior_change_scenario()
    session = scenario.session
    edit = session.edits[0]

    before_src = session.initial_files[edit.relative_path]
    after_src = edit.content
    assert after_src is not None

    before_ns: dict = {}
    exec(before_src, before_ns)
    after_ns: dict = {}
    exec(after_src, after_ns)

    before_fn = next(value for value in before_ns.values() if callable(value))
    after_fn = next(value for value in after_ns.values() if callable(value))

    with pytest.raises(ZeroDivisionError):
        before_fn([])
    assert after_fn([]) == 0
