"""
Plugin interface (BUILD PROMPT section 4.1): each plugin exposes a single
execute(params: dict) -> PluginResult function and is the ONLY code in Burns
OS allowed to hold/use the real credential for its own external system - an
agent never sees an API key, only ever calls the Gateway with an action name
and params, per design rule 2 (permissions enforced in code, not prompts)
and design rule 10 (secrets never appear in logs, memory, prompts, commits,
or chat - a PluginResult's `detail` field must never echo a raw credential).

Registered plugins map 1:1 to policy.yaml action names. An action with no
registered plugin fails closed (NotImplementedError) rather than silently
no-op'ing - see registry.get() below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol


@dataclass(frozen=True)
class PluginResult:
    ok: bool
    detail: str
    cost_usd: float = 0.0
    evidence_links: list[str] | None = None


class Plugin(Protocol):
    def execute(self, params: dict) -> PluginResult: ...


_REGISTRY: dict[str, Plugin] = {}


def register(action: str, plugin: Plugin) -> None:
    _REGISTRY[action] = plugin


def get(action: str) -> Plugin:
    plugin = _REGISTRY.get(action)
    if plugin is None:
        raise NotImplementedError(
            f"No plugin registered for action '{action}'. An action listed in policy.yaml must have a "
            "registered plugin (even a stub) before the Gateway will execute it - see gateway/plugins/."
        )
    return plugin


def registered_actions() -> list[str]:
    return sorted(_REGISTRY.keys())
