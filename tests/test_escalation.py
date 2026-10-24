from shadowci.escalation import EscalationPolicy
from shadowci.events import ChangeSet, CheckResult
from shadowci.providers.scripted import ScriptedProvider


def test_no_escalation_when_all_checks_pass():
    policy = EscalationPolicy(ScriptedProvider())
    changeset = ChangeSet(events=())
    results = (CheckResult(name="pytest", passed=True),)

    assert policy.maybe_escalate(changeset, results) is None


def test_escalates_on_a_single_failing_check():
    provider = ScriptedProvider(tests=["def test_regression(): ..."], fixes=["--- a/x\n+++ b/x\n"])
    policy = EscalationPolicy(provider)
    changeset = ChangeSet(events=())
    results = (
        CheckResult(name="pytest", passed=False, output="AssertionError"),
        CheckResult(name="mypy", passed=True),
    )

    escalation = policy.maybe_escalate(changeset, results)

    assert escalation is not None
    assert escalation.failing_checks == (results[0],)
    assert escalation.regression_test == "def test_regression(): ..."
    assert escalation.proposed_fix == "--- a/x\n+++ b/x\n"


def test_only_failing_checks_are_passed_to_the_provider():
    seen: dict[str, tuple[CheckResult, ...]] = {}

    class RecordingProvider(ScriptedProvider):
        def write_regression_test(self, changeset, check_results):
            seen["test"] = check_results
            return super().write_regression_test(changeset, check_results)

        def propose_fix(self, changeset, check_results):
            seen["fix"] = check_results
            return super().propose_fix(changeset, check_results)

    policy = EscalationPolicy(RecordingProvider())
    changeset = ChangeSet(events=())
    passing = CheckResult(name="mypy", passed=True)
    failing = CheckResult(name="pytest", passed=False, output="boom")

    policy.maybe_escalate(changeset, (passing, failing))

    assert seen["test"] == (failing,)
    assert seen["fix"] == (failing,)
