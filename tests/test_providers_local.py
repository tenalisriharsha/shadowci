import io
import json
import urllib.request
from pathlib import Path

import pytest

from shadowci.events import ChangeKind, ChangeSet, CheckResult, FileChangeEvent
from shadowci.providers.local import LocalModelProvider, LocalModelProviderError

_CHANGESET = ChangeSet(
    events=(FileChangeEvent(path=Path("src/app.py"), kind=ChangeKind.MODIFIED),)
)
_FAILING = (CheckResult(name="pytest", passed=False, output="AssertionError: boom"),)


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_write_regression_test_returns_model_response(monkeypatch):
    body = json.dumps({"response": "def test_x(): ..."}).encode("utf-8")

    def _fake_urlopen(request, timeout):
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    test_source = provider.write_regression_test(_CHANGESET, _FAILING)

    assert test_source == "def test_x(): ..."


def test_propose_fix_sends_the_configured_model_and_prompt(monkeypatch):
    captured: dict = {}

    def _fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["url"] = request.full_url
        return _FakeResponse(json.dumps({"response": "--- a/x\n+++ b/x\n"}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider(model="codellama", base_url="http://localhost:9999")

    fix = provider.propose_fix(_CHANGESET, _FAILING)

    assert fix == "--- a/x\n+++ b/x"
    assert captured["payload"]["model"] == "codellama"
    assert "app.py" in captured["payload"]["prompt"]
    assert captured["url"] == "http://localhost:9999/api/generate"


def test_unreachable_server_raises_provider_error(monkeypatch):
    def _fake_urlopen(request, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    with pytest.raises(LocalModelProviderError, match="unreachable"):
        provider.write_regression_test(_CHANGESET, _FAILING)


def test_invalid_json_response_raises_provider_error(monkeypatch):
    def _fake_urlopen(request, timeout):
        return _FakeResponse(b"not json")

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    with pytest.raises(LocalModelProviderError, match="invalid JSON"):
        provider.propose_fix(_CHANGESET, _FAILING)
