import pytest

from shadowci.evaluation.harness import (
    ChangeSetOutcome,
    EvaluationReport,
    ScenarioResult,
    run_convergence,
    run_scenario,
    run_suite,
)
from shadowci.evaluation.scenarios import (
    built_in_scenarios,
    clean_comment_scenario,
    off_by_one_bound_scenario,
    off_by_one_operator_scenario,
)
from shadowci import pipeline


def test_run_scenario_detects_an_injected_bug():
    result = run_scenario(off_by_one_operator_scenario())

    assert result.detected is True
    assert result.true_positive is True
    assert result.false_negative is False
    assert result.action == "accept"
    assert result.detection_latency_seconds is not None
    assert result.detection_latency_seconds >= 0


def test_run_scenario_stays_silent_on_a_clean_edit():
    result = run_scenario(clean_comment_scenario())

    assert result.detected is False
    assert result.true_positive is False
    assert result.false_interruption is False
    assert result.action is None
    assert result.detection_latency_seconds is None


def test_run_scenario_detected_changeset_builds_a_verified_ghost_patch():
    result = run_scenario(off_by_one_bound_scenario())

    assert len(result.changesets) >= 1
    verified = [cs for cs in result.changesets if cs.outcome.ghost_patch is not None]
    assert len(verified) == 1
    assert isinstance(verified[0], ChangeSetOutcome)
    assert verified[0].outcome.ghost_patch.evidence.all_checks_passed


def test_run_suite_aggregates_the_built_in_scenarios():
    report = run_suite(built_in_scenarios())

    bug_count = sum(1 for scenario in built_in_scenarios() if scenario.bug_expected)
    clean_count = sum(1 for scenario in built_in_scenarios() if not scenario.bug_expected)

    assert report.true_positives == bug_count
    assert report.false_negatives == 0
    assert report.false_interruptions == 0
    assert report.clean_scenario_count == clean_count
    assert report.precision == 1.0
    assert report.false_interruption_rate == 0.0
    assert report.accept_rate == 1.0
    assert report.mean_detection_latency_seconds is not None


def test_evaluation_report_metrics_are_none_with_no_data():
    report = EvaluationReport(results=())

    assert report.precision is None
    assert report.false_interruption_rate is None
    assert report.accept_rate is None
    assert report.mean_detection_latency_seconds is None


def test_run_convergence_requires_a_bug_scenario_with_test_and_fix():
    with pytest.raises(ValueError):
        run_convergence(clean_comment_scenario())


def test_run_convergence_starts_silent_and_then_surfaces():
    result = run_convergence(off_by_one_operator_scenario(), repeats=4)

    assert len(result.iterations) == 4
    assert all(iteration.verified for iteration in result.iterations)
    assert result.iterations[0].decision.interrupt is False
    assert result.iterations[1].decision.interrupt is False
    assert result.first_interrupt_iteration == 3
    assert result.iterations[2].decision.interrupt is True
    assert result.iterations[3].decision.interrupt is True


def test_run_convergence_records_an_accept_signal_per_iteration(tmp_path, monkeypatch):
    # sanity check that convergence really drives pipeline.accept_patch and
    # not a shortcut that only fakes the interruption decision
    calls: list[str] = []
    original = pipeline.accept_patch

    def _spy(root, patch_file):
        calls.append(str(patch_file))
        return original(root, patch_file)

    monkeypatch.setattr(pipeline, "accept_patch", _spy)

    run_convergence(off_by_one_operator_scenario(), repeats=3)

    assert len(calls) == 3
