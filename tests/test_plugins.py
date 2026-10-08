from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox import (
    backends,
    blobstore,
    credentials,
    observability,
    plugins,
    registry,
    storage,
)
from agent_sandbox.config import Settings
from agent_sandbox.plugins import PluginLoadError, create_from_plugin


@dataclass
class FakeEntryPoint:
    name: str
    value: Any

    def load(self) -> Any:
        return self.value


class FakeEntryPoints(tuple[FakeEntryPoint, ...]):
    def select(self, *, name: str) -> FakeEntryPoints:
        return FakeEntryPoints(item for item in self if item.name == name)


def test_plugin_factory_receives_public_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = object()
    expected = object()
    observed: list[object] = []

    def factory(value: object) -> object:
        observed.append(value)
        return expected

    monkeypatch.setattr(
        "agent_sandbox.plugins.entry_points",
        lambda *, group: FakeEntryPoints((FakeEntryPoint("private-backend", factory),)),
    )

    assert create_from_plugin("agent_sandbox.example", "Private-Backend", settings) is expected
    assert observed == [settings]


def test_missing_plugin_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_sandbox.plugins.entry_points", lambda *, group: FakeEntryPoints())

    with pytest.raises(PluginLoadError, match="not installed"):
        create_from_plugin("agent_sandbox.example", "missing", object())


def test_duplicate_plugin_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = FakeEntryPoints(
        (
            FakeEntryPoint("duplicate", lambda settings: settings),
            FakeEntryPoint("duplicate", lambda settings: settings),
        )
    )
    monkeypatch.setattr("agent_sandbox.plugins.entry_points", lambda *, group: entries)

    with pytest.raises(PluginLoadError, match="ambiguous"):
        create_from_plugin("agent_sandbox.example", "duplicate", object())


def test_non_callable_plugin_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "agent_sandbox.plugins.entry_points",
        lambda *, group: FakeEntryPoints((FakeEntryPoint("invalid", object()),)),
    )

    with pytest.raises(PluginLoadError, match="callable"):
        create_from_plugin("agent_sandbox.example", "invalid", object())


@pytest.mark.parametrize(
    ("module", "creator", "setting_name", "backend_name"),
    [
        (storage, "create_metadata_store", "metadata_backend", "private-metadata"),
        (registry, "create_worker_registry", "registry_backend", "private-registry"),
        (blobstore, "create_object_store", "object_store_backend", "private-objects"),
        (backends, "create_execution_backend", "execution_backend", "private-runtime"),
        (
            credentials,
            "create_credential_broker",
            "credential_broker",
            "private-credentials",
        ),
        (
            observability,
            "create_telemetry_sink",
            "telemetry_sink",
            "private-telemetry",
        ),
    ],
)
def test_every_external_boundary_routes_to_a_private_plugin(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    creator: str,
    setting_name: str,
    backend_name: str,
) -> None:
    expected = object()
    calls: list[tuple[str, str, Settings]] = []

    def fake_create(group: str, name: str, settings: Settings) -> object:
        calls.append((group, name, settings))
        return expected

    monkeypatch.setattr(module, "create_from_plugin", fake_create)
    settings = Settings(**{setting_name: backend_name})

    assert getattr(module, creator)(settings) is expected
    assert calls[0][1:] == (backend_name, settings)


def test_the_default_credential_broker_refuses_sensitive_env() -> None:
    """`disabled` must reject, not accept: nothing is configured to handle them.

    The selector defaults to `disabled`, and the module's own docstring says
    what that means. Returning the base environment unchanged while letting a
    caller send secrets would let them believe the deployment did something with
    those values when it did not.
    """

    broker = credentials.create_credential_broker(Settings())

    with pytest.raises(ValueError, match="not supported without a credential broker"):
        broker.validate_sensitive_keys({"GITHUB_TOKEN"})


def test_a_request_with_no_sensitive_env_is_never_refused() -> None:
    broker = credentials.create_credential_broker(Settings())

    broker.validate_sensitive_keys(set())


ROOT = Path(__file__).resolve().parents[1]


def _group_loaders() -> dict[str, set[str]]:
    """Which function passes each plugin group to `create_from_plugin`."""

    def walk(node: Any, enclosing: str | None) -> Any:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "create_from_plugin"
                and child.args
                and isinstance(child.args[0], ast.Name)
                and child.args[0].id.endswith("_GROUP")
            ):
                yield child.args[0].id, enclosing
            yield from walk(child, enclosing)

    loaders: dict[str, set[str]] = {}
    for path in (ROOT / "src").rglob("*.py"):
        for group, enclosing in walk(ast.parse(path.read_text(encoding="utf-8")), None):
            loaders.setdefault(group, set()).add(enclosing or f"<module:{path.name}>")
    return loaders


def test_every_plugin_group_is_loaded_by_one_public_factory() -> None:
    """A group loaded anywhere else escapes both the convention and the tests.

    The credential broker shipped reaching for `self.runtime.credential_broker`
    and there was nothing to reach: the group, the protocol, and the factory all
    existed, and no code path ever called the factory. Loading every group from
    one `create_*` function is what makes `test_every_external_boundary_routes_to_a_private_plugin`
    able to cover all of them, instead of covering the ones somebody remembered.
    """

    declared = {
        name for name in dir(plugins) if name.endswith("_GROUP")
    }
    loaders = _group_loaders()

    assert declared == set(loaders), (
        "every group constant needs exactly one loader; "
        f"declared but never loaded: {sorted(declared - set(loaders))}, "
        f"loaded but not declared: {sorted(set(loaders) - declared)}"
    )
    for group, names in sorted(loaders.items()):
        assert len(names) == 1, f"{group} is loaded from several places: {sorted(names)}"
        loader = names.pop()
        assert loader.startswith("create_"), (
            f"{group} is loaded by {loader!r}. Every boundary is loaded by a "
            "public `create_*` factory so the parameters it passes can be checked "
            "in one place"
        )


def test_every_plugin_factory_is_covered_by_the_boundary_test() -> None:
    """The boundary table is the list of seams somebody exercises."""

    loaders = {name for names in _group_loaders().values() for name in names}
    table = Path(__file__).read_text(encoding="utf-8")
    covered = set(re.findall(r'"(create_[a-z_]+)"', table))

    assert loaders <= covered, (
        f"{sorted(loaders - covered)} load a plugin group but are not in the "
        "boundary table, so nothing asserts they receive the settings"
    )


def test_the_adapter_guide_documents_exactly_the_groups_that_exist() -> None:
    """The table in docs/ADAPTERS.md is what a plugin author reads first.

    A group that exists in the code and not in the guide is a seam nobody
    discovers; a group in the guide and not in the code is one that does not
    exist. Both are derivable, and neither was checked — the guide could have
    named a group the code dropped, or a new seam could have shipped
    undocumented.
    """

    guide = (ROOT / "docs" / "ADAPTERS.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(agent_sandbox\.[a-z_]+)`", guide))
    declared = {
        getattr(plugins, name) for name in dir(plugins) if name.endswith("_GROUP")
    }

    assert documented, "the guide names no entry-point group, so this proves nothing"
    assert declared == documented, (
        "the guide and the plugin groups disagree; "
        f"in the code but not the guide: {sorted(declared - documented)}, "
        f"in the guide but not the code: {sorted(documented - declared)}"
    )
