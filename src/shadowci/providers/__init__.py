"""Pluggable LLM access for ShadowCI.

`LLMProvider` is the interface every backend implements. `ScriptedProvider`
is the deterministic, network-free default used in tests, CI, and demos.
Later phases add a headless `claude -p` provider and a local-model provider
behind the same interface.
"""

from shadowci.providers.base import LLMProvider
from shadowci.providers.scripted import ScriptedProvider

__all__ = ["LLMProvider", "ScriptedProvider"]
