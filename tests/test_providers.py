from shadowci.events import ChangeSet
from shadowci.providers.scripted import ScriptedProvider


def test_default_scripted_provider_returns_canned_test_and_fix():
    provider = ScriptedProvider()
    changeset = ChangeSet(events=())

    test_source = provider.write_regression_test(changeset, check_results=())
    fix_diff = provider.propose_fix(changeset, check_results=())

    assert "def test_" in test_source
    assert fix_diff.startswith("---")


def test_scripted_provider_cycles_through_custom_script():
    provider = ScriptedProvider(tests=["test A", "test B"], fixes=["fix A", "fix B"])
    changeset = ChangeSet(events=())

    assert provider.write_regression_test(changeset, ()) == "test A"
    assert provider.write_regression_test(changeset, ()) == "test B"
    # Beyond the script's length, the last entry repeats rather than erroring.
    assert provider.write_regression_test(changeset, ()) == "test B"

    assert provider.propose_fix(changeset, ()) == "fix A"
    assert provider.propose_fix(changeset, ()) == "fix B"
