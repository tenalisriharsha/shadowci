"""Local-model provider.

Talks to a local model server over HTTP (Ollama's `/api/generate` endpoint
by default) so ShadowCI can get real model-written regression tests and
fixes with no cloud API key and no network access beyond localhost. The
network call is isolated in `_complete` so tests can monkeypatch it without
a model server actually running.
"""

from __future__ import annotations

import json
import urllib.request

from shadowci.events import ChangeSet, CheckResult
from shadowci.providers.base import LLMProvider
from shadowci.providers.prompts import build_fix_prompt, build_test_prompt


class LocalModelProviderError(RuntimeError):
    """Raised when the local model server is unreachable or returns an error."""


class LocalModelProvider(LLMProvider):
    """Calls a local model server (e.g. Ollama) for test/fix generation."""

    name = "local"

    def __init__(
        self,
        model: str = "qwen2.5-coder",
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    def write_regression_test(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        return self._complete(build_test_prompt(changeset, check_results))

    def propose_fix(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        return self._complete(build_fix_prompt(changeset, check_results))

    def _complete(self, prompt: str) -> str:
        payload = json.dumps(
            {"model": self._model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                body = json.loads(response.read())
        except OSError as exc:
            raise LocalModelProviderError(
                f"local model server unreachable at {self._base_url}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise LocalModelProviderError(
                f"local model server at {self._base_url} returned invalid JSON"
            ) from exc

        return str(body.get("response", "")).strip()
