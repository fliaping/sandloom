"""Worker registry behavior against a real Redis server.

The unit suite uses a `_FakeRedis` double that only implements the calls the
registry happens to make today. These tests run the same registry against a real
server so key expiry, set membership, and decoding are actually exercised.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.registry import WorkerRegistry


@pytest.fixture
async def registry(redis_url: str) -> AsyncIterator[WorkerRegistry]:
    """A registry on an isolated namespace, cleaned up afterwards."""
    from redis.asyncio import Redis

    namespace = f"agent-sandbox-test-{time.time_ns()}"
    settings = Settings(
        internal_token="integration-token",
        redis_url=redis_url,
        registry_backend="redis",
        registry_namespace=namespace,
        heartbeat_ttl_seconds=30,
    )
    client = Redis.from_url(redis_url, decode_responses=True)
    instance = WorkerRegistry(settings, client=client)
    try:
        yield instance
    finally:
        keys = await client.keys(f"{namespace}:*")
        if keys:
            await client.delete(*keys)
        # redis-py renamed close() to aclose() in 5.x; the floor is 4.6.
        closer = getattr(client, "aclose", None) or client.close
        await closer()


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
        "profile_hash": "profile-integration",
        "observed_at": int(time.time() * 1000),
    }
    payload.update(overrides)
    return payload


async def test_heartbeat_round_trips_through_redis(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a"))

    stored = await registry.get("worker-a")

    assert stored is not None
    assert stored["worker_id"] == "worker-a"
    assert stored["capacity"] == 32
    # JSON must survive Redis without becoming a string-typed value.
    assert isinstance(stored["running_sessions"], int)


async def test_unicode_payloads_survive_the_round_trip(registry: WorkerRegistry) -> None:
    """`ensure_ascii=False` means non-ASCII bytes actually reach the server."""
    await registry.heartbeat(_heartbeat("worker-a", profile_id="编码-default"))

    stored = await registry.get("worker-a")

    assert stored is not None
    assert stored["profile_id"] == "编码-default"


async def test_selection_prefers_the_least_loaded_worker(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-busy", running_sessions=30, capacity=32))
    await registry.heartbeat(_heartbeat("worker-idle", running_sessions=1, capacity=32))

    selected = await registry.select(profile_hash="profile-integration")

    assert selected["worker_id"] == "worker-idle"


async def test_selection_skips_excluded_workers(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a", running_sessions=1))
    await registry.heartbeat(_heartbeat("worker-b", running_sessions=2))

    selected = await registry.select(profile_hash="profile-integration", exclude={"worker-a"})

    assert selected["worker_id"] == "worker-b"


async def test_selection_ignores_a_mismatched_profile(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a", profile_hash="profile-other"))

    with pytest.raises(RuntimeError, match="NO_SANDBOX_WORKER_AVAILABLE"):
        await registry.select(profile_hash="profile-integration")


async def test_selection_ignores_a_saturated_worker(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a", running_sessions=32, capacity=32))

    with pytest.raises(RuntimeError, match="NO_SANDBOX_WORKER_AVAILABLE"):
        await registry.select(profile_hash="profile-integration")


async def test_expired_key_is_pruned_from_the_worker_set(registry: WorkerRegistry) -> None:
    """A real TTL expiry, not a monkeypatched clock."""
    registry.settings = registry.settings.model_copy(update={"heartbeat_ttl_seconds": 1})
    await registry.heartbeat(_heartbeat("worker-transient"))
    assert await registry.get("worker-transient") is not None

    await asyncio.sleep(1.5)

    assert await registry.get("worker-transient") is None
    # select() must clean the dangling id out of the set.
    with pytest.raises(RuntimeError, match="NO_SANDBOX_WORKER_AVAILABLE"):
        await registry.select(profile_hash="profile-integration")
    remaining = await registry.redis.smembers(registry.worker_set_key())
    assert "worker-transient" not in remaining


async def test_unregister_removes_key_and_set_membership(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a"))

    await registry.unregister("worker-a")

    assert await registry.get("worker-a") is None
    assert await registry.redis.smembers(registry.worker_set_key()) == set()


async def test_runtime_snapshot_aggregates_a_real_fleet(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-a"))
    await registry.heartbeat(_heartbeat("worker-b", running_sessions=8, running_execs=5))

    snapshot = await registry.runtime_snapshot()

    assert snapshot["available"] is True
    assert len(snapshot["workers"]) == 2
    totals = snapshot["totals"]
    assert totals["session_capacity"] == 64
    assert totals["running_sessions"] == 12
    assert totals["running_execs"] == 7
    # Health-safe projection must not leak the endpoint.
    assert all("endpoint" not in worker for worker in snapshot["workers"])


async def test_runtime_snapshot_excludes_a_stale_heartbeat(registry: WorkerRegistry) -> None:
    await registry.heartbeat(_heartbeat("worker-fresh"))
    stale_at = int(time.time() * 1000) - 120_000
    await registry.heartbeat(_heartbeat("worker-stale", observed_at=stale_at))

    snapshot = await registry.runtime_snapshot()

    assert [item["worker_id"] for item in snapshot["workers"]] == ["worker-fresh"]
    assert snapshot["excluded_workers"] == 1


async def test_concurrent_heartbeats_all_land(registry: WorkerRegistry) -> None:
    await asyncio.gather(
        *(registry.heartbeat(_heartbeat(f"worker-{index}")) for index in range(20))
    )

    snapshot = await registry.runtime_snapshot()

    assert len(snapshot["workers"]) == 20
    assert snapshot["totals"]["session_capacity"] == 20 * 32


async def test_snapshot_bounds_a_large_fleet(registry: WorkerRegistry) -> None:
    """More workers than the fan-out bound must set `truncated`."""
    from agent_sandbox.registry import _SNAPSHOT_MAX_WORKERS

    total = _SNAPSHOT_MAX_WORKERS + 5
    # Created in batches. The assertion is about the snapshot bound, not about
    # one client holding 261 connections at once: redis-py 8 caps its pool at
    # 100 by default and raises rather than queueing, so an unbounded fan-out
    # here fails on the client library's limit rather than on the registry's
    # behaviour. The service never bursts like this — a worker heartbeats
    # itself sequentially, and a snapshot reads in batches of
    # `_SNAPSHOT_BATCH`.
    for offset in range(0, total, 32):
        await asyncio.gather(
            *(
                registry.heartbeat(_heartbeat(f"worker-{index:04d}"))
                for index in range(offset, min(offset + 32, total))
            )
        )

    snapshot = await registry.runtime_snapshot()

    assert snapshot["truncated"] is True
    assert len(snapshot["workers"]) <= _SNAPSHOT_MAX_WORKERS
