import pytest

from shadowci.providers import ClaudeCLIProvider, LocalModelProvider, ScriptedProvider, create_provider


def test_create_provider_builds_the_scripted_provider():
    assert isinstance(create_provider("scripted"), ScriptedProvider)


def test_create_provider_builds_the_claude_cli_provider():
    assert isinstance(create_provider("claude-cli"), ClaudeCLIProvider)


def test_create_provider_builds_the_local_provider():
    assert isinstance(create_provider("local"), LocalModelProvider)


def test_create_provider_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown provider"):
        create_provider("not-a-real-provider")
