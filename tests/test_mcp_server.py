from __future__ import annotations

import importlib.util
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib.metadata import version as distribution_version
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import LATEST_PROTOCOL_VERSION

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings
from agent_sandbox.mcp_server import MCP_PROXY_PATH, build_mcp_server
from agent_sandbox.models import Route


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path,
        database_url="mysql+aiomysql://user:password@localhost/agent_sandbox",
        advertise_host="test",
    )


@asynccontextmanager
async def _client(
    app: object,
    token: str,
    path: str = MCP_PROXY_PATH,
) -> AsyncIterator[Client]:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as http_client:

        @asynccontextmanager
        async def connector() -> AsyncIterator[object]:
            async with streamable_http_client(
                f"http://test{path}",
                http_client=http_client,
                terminate_on_close=False,
            ) as streams:
                yield streams

        async with Client(
            connector(),  # type: ignore[arg-type]
            mode="2026-07-28",
            read_timeout_seconds=10,
        ) as client:
            yield client


def _deployment_verifier_tools() -> set[str]:
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify-deployment.py"
    spec = importlib.util.spec_from_file_location("verify_deployment_tools", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.MCP_TOOLS)


async def test_http_mcp_uses_2026_07_28_and_exposes_sandbox_toolset(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    mcp_server = app.state.mcp_server

    async with mcp_server.session_manager.run(), _client(app, "test-token") as client:
        assert LATEST_PROTOCOL_VERSION == "2026-07-28"
        assert client.protocol_version == "2026-07-28"
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {
            "sandbox_profile",
            "sandbox_resolve",
            "sandbox_create",
            "sandbox_status",
            "sandbox_audit_get",
            "sandbox_exec",
            "sandbox_exec_status",
            "sandbox_cancel",
            "sandbox_write_file",
            "sandbox_read_file",
            "sandbox_suspend",
            "sandbox_resume",
            "sandbox_release",
        }
        # The deployment verifier pins the same toolset and fails a real
        # deployment on any difference, so the two lists move together.
        assert {tool.name for tool in tools.tools} == _deployment_verifier_tools()

        result = await client.call_tool("sandbox_profile", {})
        assert result.is_error is False
        content = dict(result.structured_content)
        # Reported as "<installed mcp version> (protocol 2026-07-28)". The
        # version is whatever the deployment installed, so asserting the literal
        # makes a correct dependency upgrade fail the suite: this test pinned
        # `2.0.0` and broke the moment `mcp` moved to 2.2.0, which the server
        # handles perfectly well.
        sdk_version = content.pop("mcp_sdk_version")
        assert sdk_version == f"{distribution_version('mcp')} (protocol 2026-07-28)"
        assert content == {
            "profile_id": "coding-default",
            "profile_hash": "bubblewrap-0.11.2-generic-v12",
            "transport": "streamable-http",
            "stateless": True,
            "worker_id": "test-8080",
            "healthy": False,
            "running_sessions": 0,
            "running_execs": 0,
            "capabilities": {},
            "orphan_reaper": {
                "last_run_at": None,
                "last_released": 0,
                "released_total": 0,
                "failures_total": 0,
                "already_gone_total": 0,
            },
        }


async def test_mcp_is_exposed_only_below_sandbox_api_prefix(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    mcp_server = app.state.mcp_server

    async with (
        mcp_server.session_manager.run(),
        _client(app, "test-token", MCP_PROXY_PATH) as client,
    ):
        assert client.protocol_version == "2026-07-28"
        result = await client.call_tool("sandbox_profile", {})
        assert result.is_error is False
        assert result.structured_content["profile_hash"].endswith("-v12")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as http_client:
        response = await http_client.post(
            "/mcp",
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert response.status_code == 404


async def test_http_mcp_rejects_missing_internal_token(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            MCP_PROXY_PATH,
            headers={
                "MCP-Protocol-Version": "2026-07-28",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )

    assert response.status_code == 401
    assert response.json() == {"error": "internal service authentication failed"}


async def test_mcp_sandbox_tools_reuse_control_plane_operations(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="scope-123",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=7,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-v9",
        status="READY",
        storage_mode="shared",
        last_active_at=now,
        created_at=now - timedelta(minutes=10),
        generation_started_at=now - timedelta(minutes=5),
        generation_created_by="workspace:scope-123",
        ready_at=now - timedelta(minutes=4),
        last_released_generation=5,
        last_released_at=now - timedelta(minutes=6),
        last_release_reason="IDLE_TIMEOUT",
        last_released_by="system:orphan-reaper",
        last_lifetime_ms=120000,
        lifecycle_count=2,
        total_lifetime_ms=120000,
        lifecycle_history=[
            {
                "generation": 5,
                "started_at": (now - timedelta(minutes=8)).isoformat(),
                "created_by": "workspace:scope-123",
                "ready_at": (now - timedelta(minutes=7)).isoformat(),
                "released_at": (now - timedelta(minutes=6)).isoformat(),
                "release_reason": "IDLE_TIMEOUT",
                "released_by": "system:orphan-reaper",
                "lifetime_ms": 120000,
            }
        ],
    )
    calls: list[dict[str, Any]] = []

    class FakeDatabase:
        async def find_route(self, sandbox_id: str) -> Route | None:
            return route if sandbox_id == route.sandbox_id else None

    class FakeService:
        worker_id = "worker-1"
        healthy = True
        runtime = SimpleNamespace(sandboxes={}, processes={})
        capabilities: ClassVar[dict[str, object]] = {"bubblewrap": True}
        reaper_status: ClassVar[dict[str, object]] = {"released_total": 2}

        async def resolve(self, request: object) -> object:
            calls.append({"operation": "resolve", "request": request})
            return request

        async def proxy_to_worker(self, **kwargs: Any) -> httpx.Response:
            calls.append({"operation": "proxy", **kwargs})
            path = str(kwargs["internal_path"])
            method = str(kwargs["method"])
            if path.endswith("/exec/exec-1") and method == "GET":
                payload: dict[str, object] = {
                    "exec_id": "exec-1",
                    "status": "SUCCEEDED",
                    "exit_code": 0,
                    "stdout": "ok\n",
                }
            elif path.endswith("/cancel"):
                payload = {"cancelled": True}
            elif path.endswith("/files") and method == "GET":
                payload = {"path": "/workspace/a.txt", "content_base64": "YQ=="}
            elif path.endswith("/exec"):
                payload = {
                    "exec_id": "exec-1",
                    "status": "SUCCEEDED",
                    "exit_code": 0,
                    "stdout": "ok\n",
                }
            else:
                payload = {"status": "READY"}
            return httpx.Response(
                200,
                json=payload,
                request=httpx.Request("GET", "http://worker.test"),
            )

        async def release(self, released_route: Route) -> None:
            calls.append({"operation": "release", "route": released_route})

    server = build_mcp_server(
        Settings(
            internal_token="token",
            local_root=tmp_path,
            profile_hash="profile-v9",
        ),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeService(),  # type: ignore[arg-type]
    )

    async with Client(server, mode="2026-07-28") as client:
        resolved = await client.call_tool(
            "sandbox_resolve",
            {
                "sandbox_id": "sb-123",
                "workspace_scope_id": "scope-123",
                "profile": "coding-default",
            },
        )
        assert resolved.structured_content["profile_hash"] == "profile-v9"
        created = await client.call_tool(
            "sandbox_create", {"sandbox_id": "sb-123", "generation": 7}
        )
        assert created.structured_content["status"] == "READY"
        status = await client.call_tool("sandbox_status", {"sandbox_id": "sb-123"})
        assert status.structured_content["generation"] == 7
        audit = await client.call_tool("sandbox_audit_get", {"sandbox_id": "sb-123"})
        assert audit.structured_content["generation_created_by"] == "workspace:scope-123"
        assert audit.structured_content["last_release_reason"] == "IDLE_TIMEOUT"
        assert audit.structured_content["last_lifetime_ms"] == 120000
        assert audit.structured_content["history_retention_days"] == 15
        assert audit.structured_content["history"][0]["generation"] == 5
        assert 239000 <= audit.structured_content["active_duration_ms"] <= 241000
        executed = await client.call_tool(
            "sandbox_exec",
            {
                "sandbox_id": "sb-123",
                "generation": 7,
                "exec_id": "exec-1",
                "argv": ["/bin/echo", "ok"],
            },
        )
        assert executed.structured_content["stdout"] == "ok\n"
        await client.call_tool(
            "sandbox_exec_status",
            {"sandbox_id": "sb-123", "generation": 7, "exec_id": "exec-1"},
        )
        cancelled = await client.call_tool(
            "sandbox_cancel",
            {"sandbox_id": "sb-123", "generation": 7, "exec_id": "exec-1"},
        )
        assert cancelled.structured_content == {"cancelled": True}
        await client.call_tool(
            "sandbox_write_file",
            {
                "sandbox_id": "sb-123",
                "generation": 7,
                "path": "/workspace/a.txt",
                "content_base64": "YQ==",
            },
        )
        downloaded = await client.call_tool(
            "sandbox_read_file",
            {"sandbox_id": "sb-123", "generation": 7, "path": "/workspace/a.txt"},
        )
        assert downloaded.structured_content == {
            "path": "/workspace/a.txt",
            "content_base64": "YQ==",
        }
        released = await client.call_tool("sandbox_release", {"sandbox_id": "sb-123"})
        assert released.structured_content == {"sandbox_id": "sb-123", "status": "RELEASED"}

    assert calls[0]["operation"] == "resolve"
    assert calls[-1] == {"operation": "release", "route": route}
