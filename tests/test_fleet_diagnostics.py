"""Fleet capacity diagnostics for both built-in registries."""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.registry import InMemoryWorkerRegistry, WorkerRegistry


def _settings(**overrides: Any) -> Settings:
    options: dict[str, Any] = {"internal_token": "token", "heartbeat_ttl_seconds": 30}
    options.update(overrides)
    return Settings(**options)


def _heartbeat(worker_id: str, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "worker_epoch": "epoch-a",
        "endpoint": f"http://{worker_id}:8080",
        "status": "ACTIVE",
        "capacity": 32,
        "running_sessions": 4,
        "running_execs": 2,
        "max_parallel_execs_per_sandbox": 16,
        "profile_id": "coding-default",
        "profile_hash": "profile-a",
        "observed_at": int(time.time() * 1000),
    }
    payload.update(overrides)
    return payload


class _FakeRedis:
    """Minimal async Redis double covering only the calls the registry makes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    async def sadd(self, key: str, member: str) -> None:
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key: str, member: str) -> None:
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def close(self) -> None:
        return None


async def test_memory_registry_aggregates_capacity() -> None:
    registry = InMemoryWorkerRegistry(_settings())
    await registry.heartbeat(_heartbeat("worker-a"))
    await registry.heartbeat(_heartbeat("worker-b", running_sessions=8, running_execs=5))

    snapshot = await registry.runtime_snapshot()

    assert snapshot["available"] is True
    assert snapshot["truncated"] is False
    assert len(snapshot["workers"]) == 2
    totals = snapshot["totals"]
    assert totals["session_capacity"] == 64
    assert totals["running_sessions"] == 12
    assert totals["remaining_sessions"] == 52
    assert totals["running_execs"] == 7
    # 12 live sessions, each allowed 16 concurrent commands.
    assert totals["existing_session_exec_capacity"] == 192
    assert totals["existing_session_free_execs"] == 185


async def test_draining_workers_do_not_offer_remaining_sessions() -> None:
    registry = InMemoryWorkerRegistry(_settings())
    await registry.heartbeat(_heartbeat("worker-a", status="DRAINING"))

    totals = (await registry.runtime_snapshot())["totals"]

    assert totals["remaining_sessions"] == 0
    # A draining worker still reports its running work.
    assert totals["running_sessions"] == 4


async def test_empty_fleet_reports_no_totals() -> None:
    snapshot = await InMemoryWorkerRegistry(_settings()).runtime_snapshot()

    assert snapshot["workers"] == []
    assert snapshot["totals"] is None
    assert snapshot["queue_length"] is None


async def test_expired_memory_workers_are_excluded() -> None:
    registry = InMemoryWorkerRegistry(_settings(heartbeat_ttl_seconds=30))
    await registry.heartbeat(_heartbeat("worker-a"))
    # Force expiry without waiting on the wall clock.
    registry._workers["worker-a"] = (time.monotonic() - 1, registry._workers["worker-a"][1])

    snapshot = await registry.runtime_snapshot()

    assert snapshot["workers"] == []
    assert snapshot["excluded_workers"] == 1


async def test_snapshot_exposes_only_health_safe_fields() -> None:
    registry = InMemoryWorkerRegistry(_settings())
    await registry.heartbeat(_heartbeat("worker-a", disk={"path": "/srv/secret"}))

    worker = (await registry.runtime_snapshot())["workers"][0]

    assert "disk" not in worker
    assert "endpoint" not in worker
    assert worker["worker_id"] == "worker-a"


async def test_redis_registry_aggregates_capacity() -> None:
    registry = WorkerRegistry(_settings(), client=_FakeRedis())
    await registry.heartbeat(_heartbeat("worker-a"))
    await registry.heartbeat(_heartbeat("worker-b"))

    snapshot = await registry.runtime_snapshot()

    assert len(snapshot["workers"]) == 2
    assert snapshot["totals"]["session_capacity"] == 64


async def test_redis_registry_excludes_stale_heartbeats() -> None:
    registry = WorkerRegistry(_settings(heartbeat_ttl_seconds=30), client=_FakeRedis())
    await registry.heartbeat(_heartbeat("worker-fresh"))
    stale_at = int(time.time() * 1000) - 60_000
    await registry.heartbeat(_heartbeat("worker-stale", observed_at=stale_at))

    snapshot = await registry.runtime_snapshot()

    assert [item["worker_id"] for item in snapshot["workers"]] == ["worker-fresh"]
    assert snapshot["excluded_workers"] == 1


async def test_redis_registry_excludes_ids_without_a_payload() -> None:
    """An expired key can leave its id behind in the worker set."""
    client = _FakeRedis()
    registry = WorkerRegistry(_settings(), client=client)
    await registry.heartbeat(_heartbeat("worker-a"))
    await client.delete(registry.key("worker-a"))

    snapshot = await registry.runtime_snapshot()

    assert snapshot["workers"] == []
    assert snapshot["excluded_workers"] == 1


@pytest.mark.parametrize("missing", ["running_execs", "max_parallel_execs_per_sandbox"])
async def test_older_workers_without_new_counters_are_still_counted(missing: str) -> None:
    """A rolling deploy must not make the whole snapshot unavailable."""
    registry = InMemoryWorkerRegistry(_settings())
    payload = _heartbeat("worker-old")
    payload.pop(missing)
    await registry.heartbeat(payload)

    totals = (await registry.runtime_snapshot())["totals"]

    assert totals["session_capacity"] == 32
    assert totals["existing_session_free_execs"] >= 0


async def test_diagnostics_endpoint_requires_auth_and_returns_snapshot(tmp_path) -> None:
    import httpx

    from agent_sandbox.app import create_app

    app = create_app(
        Settings(
            internal_token="token",
            local_root=tmp_path,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
            registry_backend="memory",
        )
    )
    await app.state.sandbox_service.registry.heartbeat(_heartbeat("worker-a"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unauthorized = await client.get("/api/v1/sandboxes/diagnostics/runtime")
        response = await client.get(
            "/api/v1/sandboxes/diagnostics/runtime",
            headers={"Authorization": "Bearer token"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json()["totals"]["session_capacity"] == 32
