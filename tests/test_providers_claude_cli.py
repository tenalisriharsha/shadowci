import re
import subprocess
from pathlib import Path

import pytest

from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.providers.claude_cli import ClaudeCLIProvider, ClaudeCLIProviderError

_CHANGESET = ChangeSet(
    events=(FileChangeEvent(path=Path("src/app.py"), kind=ChangeKind.MODIFIED),)
)
_FAILING = (CheckResult(name="pytest", passed=False, output="AssertionError: boom"),)


def _fake_run(stdout: str = "", returncode: int = 0):
    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return _run


def test_write_regression_test_returns_model_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="def test_x(): ...\n"))
    provider = ClaudeCLIProvider()

    test_source = provider.write_regression_test(_CHANGESET, _FAILING)

    assert test_source == "def test_x(): ..."


def test_propose_fix_returns_model_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout="--- a/x\n+++ b/x\n"))
    provider = ClaudeCLIProvider()

    fix = provider.propose_fix(_CHANGESET, _FAILING)

    assert fix == "--- a/x\n+++ b/x"


def test_binary_not_found_raises_provider_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)
    provider = ClaudeCLIProvider(binary="claude-does-not-exist")

    with pytest.raises(ClaudeCLIProviderError, match="not found on PATH"):
        provider.write_regression_test(_CHANGESET, _FAILING)


def test_timeout_raises_provider_error(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _raise)
    provider = ClaudeCLIProvider(timeout_seconds=1.0)

    with pytest.raises(ClaudeCLIProviderError, match="timed out"):
        provider.propose_fix(_CHANGESET, _FAILING)


def test_write_regression_test_strips_markdown_fence(monkeypatch):
    fenced = "```python\ndef test_average():\n    assert 1 == 1\n```"
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=fenced))
    provider = ClaudeCLIProvider()

    test_source = provider.write_regression_test(_CHANGESET, _FAILING)

    assert test_source == "def test_average():\n    assert 1 == 1"
    compile(test_source, "t.py", "exec")


def test_propose_fix_strips_diff_fence(monkeypatch):
    fenced = "```diff\n--- a/x\n+++ b/x\n```"
    monkeypatch.setattr(subprocess, "run", _fake_run(stdout=fenced))
    provider = ClaudeCLIProvider()

    fix = provider.propose_fix(_CHANGESET, _FAILING)

    assert fix.startswith("---")
    assert "```" not in fix


def test_nonzero_exit_raises_provider_error(monkeypatch):
    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError, match="auth error"):
        provider.write_regression_test(_CHANGESET, _FAILING)


def test_stdout_only_failure_message_includes_stdout_text(monkeypatch):
    # The real `claude` CLI prints its own failure messages (expired login,
    # usage limit reached) on stdout and leaves stderr empty.
    failure_text = "Failed to authenticate: OAuth session expired and could not be refreshed"

    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 1, stdout=failure_text, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError) as exc_info:
        provider.write_regression_test(_CHANGESET, _FAILING)

    assert failure_text in str(exc_info.value)


def test_expired_login_failure_includes_try_hint(monkeypatch):
    failure_text = "Failed to authenticate: OAuth session expired and could not be refreshed"

    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 1, stdout=failure_text, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError) as exc_info:
        provider.write_regression_test(_CHANGESET, _FAILING)

    message = str(exc_info.value)
    assert "Try:" in message
    assert "/login" in message


def test_usage_limit_failure_includes_try_hint(monkeypatch):
    failure_text = "Claude AI usage limit reached. Your limit will reset at 3:00 PM."

    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 1, stdout=failure_text, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError) as exc_info:
        provider.write_regression_test(_CHANGESET, _FAILING)

    message = str(exc_info.value)
    assert "Try:" in message
    assert "scripted" in message
    assert "reset" in message.lower()


def test_binary_not_found_failure_includes_try_hint(monkeypatch):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", _raise)
    provider = ClaudeCLIProvider(binary="claude-does-not-exist")

    with pytest.raises(ClaudeCLIProviderError) as exc_info:
        provider.write_regression_test(_CHANGESET, _FAILING)

    message = str(exc_info.value)
    assert "Try:" in message
    assert "install" in message.lower()
    assert "PATH" in message


def test_no_output_failure_still_names_exit_status(monkeypatch):
    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 7, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError) as exc_info:
        provider.write_regression_test(_CHANGESET, _FAILING)

    message = str(exc_info.value)
    assert "7" in message
    # The original bug produced a message ending in an empty tail after the
    # colon ("... exited 7: ") with no explanation at all.
    assert not re.search(r"exited 7:\s*$", message)
