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


def test_evaluate_save_dir_writes_scenario_json_without_running(tmp_path: Path, capsys):
    save_dir = tmp_path / "scenarios"

    exit_code = main(["evaluate", "--save-dir", str(save_dir)])
    out = capsys.readouterr().out

    assert exit_code == 0
    written = sorted(save_dir.glob("*.json"))
    assert len(written) == 5
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


def test_evaluate_repeat_reports_policy_convergence(capsys):
    exit_code = main(["evaluate", "--repeat", "4"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "replaying 'off-by-one-operator' 4 time(s)" in out
    assert "iteration 1: verified, policy stays silent" in out
    assert "the policy started surfacing this kind of fix from iteration 3 onward" in out
