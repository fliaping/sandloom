from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox import doctor
from agent_sandbox.config import Settings


async def test_non_linux_is_an_actionable_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    report = await doctor.diagnose(Settings(_env_file=None, internal_token="do-not-print"))
    assert report["can_start"] is False
    assert "Linux" in str(report["errors"])
    assert "do-not-print" not in str(report)


async def test_private_plugins_are_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    report = await doctor.diagnose(Settings(_env_file=None, execution_backend="enterprise"))
    assert report["can_start"] is False
    assert "third-party" in str(report["errors"])


async def test_failed_policy_is_not_replaced_by_successful_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.os, "geteuid", lambda: 0)
    roots = []

    class FakeRuntime:
        def __init__(self, settings: Settings) -> None:
            self.settings = settings
            roots.append(settings.workspace_root)
            assert settings.workspace_root != tmp_path
            assert settings.object_store_backend == "disabled"

        async def probe(self) -> dict[str, Any]:
            if self.settings.isolation_level == "strict":
                raise RuntimeError("strict unavailable")
            return {"selected_level": "basic"}

    monkeypatch.setattr(doctor, "SandboxRuntime", FakeRuntime)
    report = await doctor.diagnose(
        Settings(_env_file=None, isolation_level="strict", local_root=tmp_path)
    )
    assert report["can_start"] is False
    assert report["inventory"] == {"selected_level": "basic"}
    assert "strict unavailable" in str(report["errors"])
    assert all(not root.parent.exists() for root in roots)


async def test_ready_report_uses_disposable_roots_and_does_not_expose_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.os, "geteuid", lambda: 0)
    roots = []

    class FakeRuntime:
        def __init__(self, settings: Settings) -> None:
            roots.append(settings.workspace_root)
            assert settings.workspace_root != tmp_path
            assert settings.shared_root is None
            assert settings.object_store_backend == "disabled"
            assert settings.telemetry_sink is None
            assert settings.credential_broker == "disabled"

        async def probe(self) -> dict[str, Any]:
            return {"selected_level": "basic", "features": ["cgroup_namespace"]}

    monkeypatch.setattr(doctor, "SandboxRuntime", FakeRuntime)
    report = await doctor.diagnose(
        Settings(
            _env_file=None,
            isolation_level="basic",
            isolation_required_features=["cgroup_namespace"],
            local_root=tmp_path,
            shared_root=tmp_path / "shared",
            internal_token="secret-do-not-print",
            database_url="postgresql+asyncpg://private:db-password@private/db",
        )
    )
    assert report["can_start"] is True
    assert report["status"] == "ready"
    assert report["errors"] == []
    assert report["requested_policy"] == {
        "level": "basic",
        "network_mode": "host",
        "required_features": ["cgroup_namespace"],
        "optional_features": [],
    }
    assert "secret-do-not-print" not in json.dumps(report)
    assert "db-password" not in json.dumps(report)
    assert all(not root.parent.exists() for root in roots)


async def test_runtime_construction_failure_is_a_report_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor.os, "geteuid", lambda: 0)

    def failed_runtime(settings: Settings) -> None:
        raise OSError("bwrap executable missing")

    monkeypatch.setattr(doctor, "SandboxRuntime", failed_runtime)
    report = await doctor.diagnose(Settings(_env_file=None))
    assert report["can_start"] is False
    assert report["errors"] == ["bwrap executable missing"]
    assert report["inventory_error"] == "bwrap executable missing"


def test_invalid_settings_json_omits_input_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def invalid_settings() -> Settings:
        return Settings(_env_file=None, isolation_level="invalid-secret-do-not-print")

    monkeypatch.setattr(doctor, "Settings", invalid_settings)
    monkeypatch.setattr(doctor.sys, "argv", ["sandloom-doctor", "--json"])
    with pytest.raises(SystemExit) as raised:
        doctor.main()
    assert raised.value.code == 2
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["can_start"] is False
    assert report["errors"][0]["field"] == ["isolation_level"]
    assert "invalid-secret-do-not-print" not in output
