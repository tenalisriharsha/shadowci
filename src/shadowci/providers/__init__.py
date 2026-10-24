"""Pluggable LLM access for ShadowCI.

`LLMProvider` is the interface every backend implements. `ScriptedProvider`
is the deterministic, network-free default used in tests, CI, and demos.
`ClaudeCLIProvider` shells out to headless `claude -p`; `LocalModelProvider`
talks to a local model server (e.g. Ollama). `create_provider` builds one
from the name in `ShadowCIConfig.provider`.
"""

from __future__ import annotations

from shadowci.providers.base import LLMProvider
from shadowci.providers.claude_cli import ClaudeCLIProvider, ClaudeCLIProviderError
from shadowci.providers.local import LocalModelProvider, LocalModelProviderError
from shadowci.providers.scripted import ScriptedProvider

_PROVIDERS: dict[str, type[LLMProvider]] = {
    "scripted": ScriptedProvider,
    "claude-cli": ClaudeCLIProvider,
    "local": LocalModelProvider,
}


def create_provider(name: str) -> LLMProvider:
    """Build the `LLMProvider` registered under `name`.

    Raises `ValueError` for an unknown name so a typo in `shadowci.toml`
    fails loudly instead of silently falling back to a different provider.
    """
    try:
        provider_cls = _PROVIDERS[name]
    except KeyError as exc:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"unknown provider '{name}' (known providers: {known})") from exc
    return provider_cls()


__all__ = [
    "LLMProvider",
    "ScriptedProvider",
    "ClaudeCLIProvider",
    "ClaudeCLIProviderError",
    "LocalModelProvider",
    "LocalModelProviderError",
    "create_provider",
]
