"""Runtime discovery for optional backend plugins.

The public package owns only the contracts and built-in portable backends.
Deployments can install private distributions that register factories through
Python entry points without adding private code or dependencies here.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any

METADATA_STORE_GROUP = "agent_sandbox.metadata_stores"
REGISTRY_GROUP = "agent_sandbox.registries"
OBJECT_STORE_GROUP = "agent_sandbox.object_stores"
EXECUTION_BACKEND_GROUP = "agent_sandbox.execution_backends"
TELEMETRY_SINK_GROUP = "agent_sandbox.telemetry_sinks"
PLUGIN_API_VERSION = 1


class PluginLoadError(RuntimeError):
    """A configured plugin is missing, ambiguous, or has an invalid factory."""


def create_from_plugin(group: str, name: str, settings: Any) -> Any:
    """Load exactly one named entry point and call its settings factory.

    Selection deliberately fails closed: a misspelled or missing private
    adapter can never silently fall back to a less suitable public backend.
    """
    normalized = name.strip().lower()
    if not normalized:
        raise PluginLoadError(f"plugin name for {group!r} cannot be empty")
    matches = tuple(entry_points(group=group).select(name=normalized))
    if len(matches) != 1:
        detail = "not installed" if not matches else "ambiguous"
        raise PluginLoadError(f"plugin {normalized!r} in {group!r} is {detail}")
    factory = matches[0].load()
    if not callable(factory):
        raise PluginLoadError(
            f"plugin {normalized!r} in {group!r} does not expose a callable factory"
        )
    return factory(settings)


__all__ = [
    "CREDENTIAL_BROKER_GROUP",
    "EXECUTION_BACKEND_GROUP",
    "METADATA_STORE_GROUP",
    "OBJECT_STORE_GROUP",
    "PLUGIN_API_VERSION",
    "REGISTRY_GROUP",
    "TELEMETRY_SINK_GROUP",
    "PluginLoadError",
    "create_from_plugin",
]
CREDENTIAL_BROKER_GROUP = "agent_sandbox.credential_brokers"
