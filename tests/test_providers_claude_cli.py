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


def test_nonzero_exit_raises_provider_error(monkeypatch):
    def _run(args, capture_output, text, timeout):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="auth error")

    monkeypatch.setattr(subprocess, "run", _run)
    provider = ClaudeCLIProvider()

    with pytest.raises(ClaudeCLIProviderError, match="auth error"):
        provider.write_regression_test(_CHANGESET, _FAILING)
