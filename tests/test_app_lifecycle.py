"""Startup failure must close partial resources; malformed auth is a 401."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings


@pytest.mark.parametrize("failure", ["connect", "start", "registry_close"])
async def test_lifespan_cleans_up_even_when_initialization_or_close_fails(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app = create_app(Settings(internal_token="token"))
    service = app.state.sandbox_service
    calls: list[str] = []

    def callback(name: str) -> Callable[..., object]:
        async def call() -> None:
            calls.append(name)
            if name == failure:
                raise RuntimeError(name)

        return call

    for owner, method, name in (
        (service.database, "connect", "connect"),
        (service.database, "close", "database_close"),
        (service.registry, "close", "registry_close"),
        (service, "start", "start"),
        (service, "stop", "stop"),
    ):
        monkeypatch.setattr(owner, method, callback(name))
    with pytest.raises(RuntimeError, match=failure):
        async with app.router.lifespan_context(app):
            pass
    if failure == "connect":
        assert calls == ["connect", "registry_close", "database_close"]
    else:
        assert calls == ["connect", "start", "stop", "registry_close", "database_close"]


async def test_non_ascii_bearer_is_rejected_without_server_error() -> None:
    app = create_app(Settings(internal_token="token"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/templates", headers={b"Authorization": b"Bearer \xff"})
    assert response.status_code == 401
