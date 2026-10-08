from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from agent_sandbox.app import create_app, relay_worker_response
from agent_sandbox.config import Settings

SECRET = "synthetic-managed-secret-do-not-echo"


@pytest.mark.parametrize(
    "path", ["/api/v1/sandboxes/sb-123/exec", "/internal/v1/sandboxes/sb-123/exec"]
)
@pytest.mark.parametrize(
    "sensitive",
    [
        # An over-long value: the raw input must never reach the response body.
        {"API_TOKEN": SECRET * 2500},
        # A non-string value: pydantic would otherwise echo the whole mapping.
        {"API_TOKEN": {SECRET: SECRET}},
        # A secret used as the key itself: dynamic map keys leak too.
        {SECRET: SECRET * 2500},
    ],
)
async def test_request_validation_never_echoes_credentials(tmp_path, caplog, path, sensitive):
    app = create_app(
        Settings(
            internal_token="test-token",
            local_root=tmp_path,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/sandbox.db",
            advertise_host="worker.test",
        )
    )
    service = app.state.sandbox_service
    service.execute = AsyncMock()
    service.proxy_to_worker = AsyncMock()
    service.validate_local_route = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            path,
            headers={
                "Authorization": "Bearer test-token",
                "X-Sandbox-Worker-ID": "worker.test-8080",
            },
            json={
                "exec_id": "exec-1",
                "generation": 1,
                "argv": ["/bin/true"],
                "env": sensitive,
            },
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SANDBOX_INVALID_REQUEST"
    assert SECRET not in response.text and SECRET not in caplog.text
    assert "input" not in response.json()["detail"]
    service.execute.assert_not_awaited()
    service.proxy_to_worker.assert_not_awaited()


def test_validation_response_keeps_known_field_names():
    response = relay_worker_response(
        httpx.Response(
            422,
            json={"detail": [{"loc": ["body", "env", SECRET], "msg": SECRET, "type": SECRET}]},
        )
    )
    assert b"env" in response.body


def test_control_plane_sanitizes_older_worker_validation_response():
    raw = httpx.Response(
        422,
        json={
            "detail": [
                {
                    "loc": ["body", "env", SECRET],
                    "msg": SECRET,
                    "type": SECRET,
                    "input": {"token": SECRET},
                    "ctx": {"error": SECRET},
                }
            ]
        },
    )
    response = relay_worker_response(raw)
    assert response.status_code == 422
    assert SECRET.encode() not in response.body
    assert b"env" in response.body


def test_unknown_field_names_are_replaced_with_placeholder():
    raw = httpx.Response(
        422,
        json={"detail": [{"loc": ["body", SECRET], "msg": SECRET, "type": SECRET}]},
    )
    response = relay_worker_response(raw)
    assert SECRET.encode() not in response.body
    assert b"request" in response.body


def test_successful_worker_output_is_not_rewritten():
    raw = httpx.Response(200, json={"stdout": "ordinary business output", "exit_code": 0})
    response = relay_worker_response(raw)
    assert response.body == raw.content
