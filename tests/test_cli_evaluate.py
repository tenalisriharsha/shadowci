import json
from pathlib import Path

from shadowci.cli import main


def test_evaluate_runs_the_built_in_suite_and_reports_metrics(capsys):
    exit_code = main(["evaluate"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "off-by-one-operator [bug]: DETECTED" in out
    assert "clean-rename [clean]: SILENT (correct)" in out
    assert "precision" in out
    assert "false interruption rate" in out
    assert "accept rate" in out
    assert "mean detection latency" in out
    assert "reverted_user_change" in out


def test_evaluate_save_dir_writes_scenario_json_without_running(tmp_path: Path, capsys):
    save_dir = tmp_path / "scenarios"

    exit_code = main(["evaluate", "--save-dir", str(save_dir)])
    out = capsys.readouterr().out

    assert exit_code == 0
    written = sorted(save_dir.glob("*.json"))
    # Issue #36 adds two crash-type scenarios (garbage-code,
    # garbage-code-import-error) to the built-in suite, growing it from 5 to 7.
    assert len(written) == 7
    assert "wrote" in out

    data = json.loads(written[0].read_text())
    assert "session" in data
    assert "bug_expected" in data


def test_evaluate_replays_a_single_saved_scenario(tmp_path: Path, capsys):
    save_dir = tmp_path / "scenarios"
    main(["evaluate", "--save-dir", str(save_dir)])
    capsys.readouterr()

    scenario_path = save_dir / "off-by-one-operator.json"
    exit_code = main(["evaluate", "--scenario", str(scenario_path)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "off-by-one-operator [bug]: DETECTED" in out
    assert "scenarios run           : 1" in out


def test_evaluate_reports_the_garbage_code_scenarios_as_detected(capsys):
    # Issue #36: a crash-type scenario (broken code, not a logic bug) must
    # still show up in the default `evaluate` run and be detected, so it is
    # part of the regression guard issue #37's revert check must not break.
    from shadowci.evaluation.scenarios import (
        garbage_code_import_error_scenario,
        garbage_code_scenario,
    )

    exit_code = main(["evaluate"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert f"{garbage_code_scenario().session.name} [bug]: DETECTED" in out
    assert f"{garbage_code_import_error_scenario().session.name} [bug]: DETECTED" in out


def test_evaluate_repeat_reports_policy_convergence(capsys):
    exit_code = main(["evaluate", "--repeat", "4"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "replaying 'off-by-one-operator' 4 time(s)" in out
    assert "iteration 1: verified, policy stays silent" in out
    assert "the policy started surfacing this kind of fix from iteration 3 onward" in out
