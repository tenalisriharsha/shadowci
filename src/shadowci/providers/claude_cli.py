"""Headless `claude -p` provider.

Shells out to the `claude` CLI in headless/print mode (`claude -p <prompt>`)
so ShadowCI can get real model-written regression tests and fixes without
embedding an SDK dependency or an API key of its own — it rides on whatever
`claude` is already authenticated with on the machine it runs on.
"""

from __future__ import annotations

import subprocess

from shadowci.events import ChangeSet, CheckResult
from shadowci.providers.base import LLMProvider
from shadowci.providers.prompts import build_fix_prompt, build_test_prompt


class ClaudeCLIProviderError(RuntimeError):
    """Raised when the headless `claude -p` invocation fails or is unavailable."""


class ClaudeCLIProvider(LLMProvider):
    """Calls `claude -p` headlessly for test/fix generation."""

    name = "claude-cli"

    def __init__(self, binary: str = "claude", timeout_seconds: float = 120.0) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    def write_regression_test(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        return self._run(build_test_prompt(changeset, check_results))

    def propose_fix(
        self, changeset: ChangeSet, check_results: tuple[CheckResult, ...]
    ) -> str:
        return self._run(build_fix_prompt(changeset, check_results))

    def _run(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                [self._binary, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ClaudeCLIProviderError(f"'{self._binary}' not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCLIProviderError(
                f"'{self._binary} -p' timed out after {self._timeout_seconds}s"
            ) from exc

        if result.returncode != 0:
            raise ClaudeCLIProviderError(
                f"'{self._binary} -p' exited {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout.strip()
