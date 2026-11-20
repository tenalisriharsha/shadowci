import io
import json
import urllib.error
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


def test_write_regression_test_strips_markdown_code_fence(monkeypatch):
    fenced = "```python\ndef test_x():\n    assert True\n```"
    body = json.dumps({"response": fenced}).encode("utf-8")

    def _fake_urlopen(request, timeout):
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    test_source = provider.write_regression_test(_CHANGESET, _FAILING)

    assert test_source == "def test_x():\n    assert True"


def test_propose_fix_strips_markdown_code_fence(monkeypatch):
    fenced = "```diff\n--- a/x\n+++ b/x\n```"
    body = json.dumps({"response": fenced}).encode("utf-8")

    def _fake_urlopen(request, timeout):
        return _FakeResponse(body)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    fix = provider.propose_fix(_CHANGESET, _FAILING)

    assert fix == "--- a/x\n+++ b/x"


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


def test_http_error_from_reachable_server_reports_status_not_unreachable(monkeypatch):
    def _fake_urlopen(request, timeout):
        fp = io.BytesIO(b'{"error":"model \\"qwen2.5-coder\\" not found, try pulling it first"}')
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, fp)

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    provider = LocalModelProvider()

    with pytest.raises(LocalModelProviderError) as excinfo:
        provider.write_regression_test(_CHANGESET, _FAILING)

    message = str(excinfo.value)
    assert "404" in message
    assert "unreachable" not in message


def test_connection_failure_still_raises_unreachable(monkeypatch):
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
