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
from shadowci.providers.fences import strip_code_fence
from shadowci.providers.prompts import build_fix_prompt, build_test_prompt


class ClaudeCLIProviderError(RuntimeError):
    """Raised when the headless `claude -p` invocation fails or is unavailable."""


_MAX_OUTPUT_LINES = 5


def _combine_output(stdout: str, stderr: str) -> str:
    """Join stdout/stderr into one trimmed blurb, since `claude -p` prints its
    own failure messages (expired login, usage limit) on stdout and leaves
    stderr empty."""
    parts = [part.strip() for part in (stdout, stderr) if part.strip()]
    lines = "\n".join(parts).splitlines()
    if len(lines) > _MAX_OUTPUT_LINES:
        lines = lines[:_MAX_OUTPUT_LINES] + ["... (truncated)"]
    return "\n".join(lines)


def _try_hint(output: str) -> str | None:
    """Return a `Try:` hint for failure text recognised by its wording."""
    lowered = output.lower()
    if "usage limit" in lowered:
        return (
            'Try: wait for the reset time in the message above, or set '
            'provider = "scripted".'
        )
    if "authenticate" in lowered or "oauth" in lowered or "login" in lowered:
        return "Try: run `claude`, then `/login`."
    return None


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

    def health_check(self, timeout_seconds: float = 30.0) -> str:
        """One trivial `claude -p` call, used by `shadowci doctor` to confirm
        the CLI can answer. Raises `ClaudeCLIProviderError` with the real
        output on failure, same as a normal invocation."""
        probe = ClaudeCLIProvider(self._binary, timeout_seconds=timeout_seconds)
        return probe._run("Reply with the single word OK.")

    def _run(self, prompt: str) -> str:
        try:
            result = subprocess.run(
                [self._binary, "-p", prompt],
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as exc:
            raise ClaudeCLIProviderError(
                f"'{self._binary}' not found on PATH\n"
                "Try: install Claude Code, or make sure the `claude` "
                "command is on your PATH."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCLIProviderError(
                f"'{self._binary} -p' timed out after {self._timeout_seconds}s"
            ) from exc

        if result.returncode != 0:
            output = _combine_output(result.stdout, result.stderr)
            message = f"'{self._binary} -p' exited {result.returncode}"
            message += f": {output}" if output else " with no output"
            hint = _try_hint(output)
            if hint:
                message += f"\n{hint}"
            raise ClaudeCLIProviderError(message)
        return strip_code_fence(result.stdout.strip())
