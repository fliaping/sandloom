from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings
from agent_sandbox.models import Route


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path,
        database_url="mysql+aiomysql://user:password@localhost/agent_sandbox",
        advertise_host="worker.test",
    )


async def test_existing_internal_exec_remains_available(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    service = app.state.sandbox_service
    service.validate_local_route = AsyncMock()
    service.execute = AsyncMock(
        return_value={"exec_id": "exec-1", "status": "SUCCEEDED", "exit_code": 0}
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/internal/v1/sandboxes/sb-123/exec",
            headers={
                "Authorization": "Bearer test-token",
                "X-Sandbox-Worker-ID": "worker.test-8080",
            },
            json={"exec_id": "exec-1", "generation": 2, "argv": ["/bin/true"]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    service.validate_local_route.assert_awaited_once_with("sb-123", 2)


async def test_proxy_exec_forwards_without_client_worker_headers(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    proxy = AsyncMock(
        return_value=httpx.Response(
            200,
            json={
                "exec_id": "exec-1",
                "status": "SUCCEEDED",
                "exit_code": 0,
                "stdout": "ok\n",
                "stderr": "",
                "duration_ms": 12,
                "truncated": False,
            },
        )
    )
    app.state.sandbox_service.proxy_to_worker = proxy

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/sandboxes/sb-123/exec",
            headers={"Authorization": "Bearer test-token"},
            json={"exec_id": "exec-1", "generation": 2, "argv": ["/bin/echo", "ok"]},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "SUCCEEDED"
    proxy.assert_awaited_once_with(
        sandbox_id="sb-123",
        generation=2,
        method="POST",
        internal_path="/internal/v1/sandboxes/sb-123/exec",
        json_body={
            "exec_id": "exec-1",
            "generation": 2,
            "argv": ["/bin/echo", "ok"],
            "cwd": "/workspace",
            "env": {},
            "sensitive_env": {},
            "timeout_seconds": None,
            "background": False,
            "exec_scope": None,
        },
        timeout_seconds=320.0,
    )


async def test_audit_endpoint_returns_lifecycle_summary(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    app = create_app(_settings(tmp_path))
    route = Route(
        sandbox_id="sb-audit",
        workspace_scope_id="scope-1",
        worker_id="worker.test-8080",
        worker_epoch="epoch-1",
        generation=4,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-v9",
        status="RELEASED",
        storage_mode="local",
        last_active_at=now,
        created_at=now - timedelta(minutes=10),
        generation_started_at=now - timedelta(minutes=5),
        generation_created_by="workspace:scope-1",
        ready_at=now - timedelta(minutes=4),
        last_released_generation=3,
        last_released_at=now,
        last_release_reason="CLIENT_RELEASE",
        last_released_by="workspace:scope-1",
        last_lifetime_ms=240000,
        lifecycle_count=2,
        total_lifetime_ms=300000,
        lifecycle_history=[
            {
                "generation": 3,
                "started_at": (now - timedelta(minutes=5)).isoformat(),
                "created_by": "workspace:scope-1",
                "ready_at": (now - timedelta(minutes=4)).isoformat(),
                "released_at": now.isoformat(),
                "release_reason": "CLIENT_RELEASE",
                "released_by": "workspace:scope-1",
                "lifetime_ms": 240000,
            }
        ],
    )
    app.state.sandbox_service.database.find_route = AsyncMock(return_value=route)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/sandboxes/sb-audit/audit",
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 200
    assert response.json()["generation_created_by"] == "workspace:scope-1"
    assert response.json()["last_release_reason"] == "CLIENT_RELEASE"
    assert response.json()["last_lifetime_ms"] == 240000
    assert response.json()["active_duration_ms"] is None
    assert response.json()["history_retention_days"] == 15
    assert response.json()["history"][0]["generation"] == 3
