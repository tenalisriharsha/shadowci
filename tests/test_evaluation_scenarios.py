from pathlib import Path

from shadowci.evaluation.scenarios import (
    built_in_scenarios,
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
    for scenario in built_in_scenarios():
        if scenario.bug_expected:
            assert scenario.regression_test
            assert scenario.proposed_fix
            assert len(scenario.session.edits) == 1


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
