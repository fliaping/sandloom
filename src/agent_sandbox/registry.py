"""Worker registration and load-based selection."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Protocol, cast

from .config import Settings
from .plugins import REGISTRY_GROUP, create_from_plugin

_SNAPSHOT_FIELDS = (
    "worker_id",
    "status",
    "capacity",
    "running_sessions",
    "running_execs",
    "max_parallel_execs_per_sandbox",
    "observed_at",
    "profile_id",
)
# Bound the fan-out so one diagnostics call cannot walk an unbounded fleet.
_SNAPSHOT_MAX_WORKERS = 256
_SNAPSHOT_BATCH = 16

_SNAPSHOT_NOTE = (
    "Only workers with a fresh heartbeat are counted. Sessions and commands use "
    "different units: command concurrency is capped per sandbox and free slots "
    "cannot be borrowed across sandboxes. Requests over the limit are rejected; "
    "there is no persistent command queue."
)


def _snapshot_worker(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a heartbeat onto health-safe fields, defaulting missing counters."""
    return {key: payload.get(key) for key in _SNAPSHOT_FIELDS}


def _snapshot_totals(workers: list[dict[str, Any]]) -> dict[str, int] | None:
    if not workers:
        return None

    def _as_int(item: dict[str, Any], key: str) -> int:
        try:
            return int(item.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    capacity = sum(_as_int(item, "capacity") for item in workers)
    sessions = sum(_as_int(item, "running_sessions") for item in workers)
    execs = sum(_as_int(item, "running_execs") for item in workers)
    # Existing sessions can each hold up to the per-sandbox limit, so this is
    # the command headroom without creating any new sandbox.
    exec_limit = sum(
        _as_int(item, "running_sessions") * _as_int(item, "max_parallel_execs_per_sandbox")
        for item in workers
    )
    return {
        "session_capacity": capacity,
        "running_sessions": sessions,
        "remaining_sessions": sum(
            max(0, _as_int(item, "capacity") - _as_int(item, "running_sessions"))
            for item in workers
            if item.get("status") == "ACTIVE"
        ),
        "running_execs": execs,
        "existing_session_exec_capacity": exec_limit,
        "existing_session_free_execs": max(0, exec_limit - execs),
    }


class Registry(Protocol):
    async def close(self) -> None: ...
    async def heartbeat(self, payload: dict[str, Any]) -> None: ...
    async def unregister(self, worker_id: str) -> None: ...
    async def get(self, worker_id: str | None) -> dict[str, Any] | None: ...
    async def select(
        self,
        *,
        profile_hash: str,
        exclude: set[str] | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]: ...
    async def runtime_snapshot(self) -> dict[str, Any]: ...


class WorkerRegistry:
    """Distributed Redis registry adapter."""

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self.settings = settings
        if client is None:
            from redis.asyncio import Redis

            client = Redis.from_url(settings.redis_url, decode_responses=True)
        self.redis = client

    def worker_set_key(self) -> str:
        return f"{self.settings.registry_namespace}:workers"

    def key(self, worker_id: str) -> str:
        return f"{self.settings.registry_namespace}:worker:{worker_id}"

    async def close(self) -> None:
        await self.redis.close()

    async def runtime_snapshot(self) -> dict[str, Any]:
        """Read-only fleet capacity observation; stale heartbeats are excluded."""
        ids = sorted(await self.redis.smembers(self.worker_set_key()))
        workers: list[dict[str, Any]] = []
        excluded = 0
        now = int(time.time() * 1000)
        ttl_ms = self.settings.heartbeat_ttl_seconds * 1000
        for offset in range(0, min(len(ids), _SNAPSHOT_MAX_WORKERS), _SNAPSHOT_BATCH):
            batch = await asyncio.gather(
                *(
                    self.get(value.decode() if isinstance(value, bytes) else str(value))
                    for value in ids[offset : offset + _SNAPSHOT_BATCH]
                )
            )
            for item in batch:
                # A worker whose key expired but whose id lingers in the set is
                # not evidence of live capacity.
                if item is None or not 0 <= now - int(item.get("observed_at") or 0) < ttl_ms:
                    excluded += 1
                    continue
                workers.append(_snapshot_worker(item))
        return {
            "available": True,
            "scope": "observed_workers",
            "workers": workers,
            "excluded_workers": excluded,
            "truncated": len(ids) > _SNAPSHOT_MAX_WORKERS,
            "totals": _snapshot_totals(workers),
            "queue_length": None,
            "note": _SNAPSHOT_NOTE,
        }

    async def heartbeat(self, payload: dict[str, Any]) -> None:
        worker_id = str(payload["worker_id"])
        await self.redis.set(
            self.key(worker_id),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ex=self.settings.heartbeat_ttl_seconds,
        )
        await self.redis.sadd(self.worker_set_key(), worker_id)

    async def unregister(self, worker_id: str) -> None:
        await self.redis.delete(self.key(worker_id))
        await self.redis.srem(self.worker_set_key(), worker_id)

    async def get(self, worker_id: str | None) -> dict[str, Any] | None:
        if not worker_id:
            return None
        raw = await self.redis.get(self.key(worker_id))
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(raw) if raw else None

    async def select(
        self,
        *,
        profile_hash: str,
        exclude: set[str] | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        excluded = exclude or set()
        workers: list[dict[str, Any]] = []
        worker_ids = await self.redis.smembers(self.worker_set_key())
        for worker_id_value in worker_ids:
            worker_id = (
                worker_id_value.decode() if isinstance(worker_id_value, bytes) else worker_id_value
            )
            raw = await self.redis.get(self.key(str(worker_id)))
            if not raw:
                await self.redis.srem(self.worker_set_key(), str(worker_id))
                continue
            if isinstance(raw, bytes):
                raw = raw.decode()
            item = json.loads(raw)
            if (
                item.get("worker_id") not in excluded
                and item.get("status") == "ACTIVE"
                and item.get("profile_hash") == profile_hash
                and int(item.get("running_sessions", 0)) < int(item.get("capacity", 0))
                and (environment is None or item.get("environment") == environment)
            ):
                workers.append(item)
        if not workers:
            raise RuntimeError("NO_SANDBOX_WORKER_AVAILABLE")
        return min(
            workers,
            key=lambda item: (
                int(item.get("running_sessions", 0)) / max(int(item.get("capacity", 1)), 1)
            ),
        )


class InMemoryWorkerRegistry:
    """Zero-dependency registry for one service process hosting many sandboxes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._workers: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        self._workers.clear()

    async def heartbeat(self, payload: dict[str, Any]) -> None:
        expires_at = time.monotonic() + self.settings.heartbeat_ttl_seconds
        async with self._lock:
            self._workers[str(payload["worker_id"])] = (expires_at, dict(payload))

    async def unregister(self, worker_id: str) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)

    async def get(self, worker_id: str | None) -> dict[str, Any] | None:
        if not worker_id:
            return None
        async with self._lock:
            self._prune()
            entry = self._workers.get(worker_id)
            return dict(entry[1]) if entry else None

    async def select(
        self,
        *,
        profile_hash: str,
        exclude: set[str] | None = None,
        environment: str | None = None,
    ) -> dict[str, Any]:
        excluded = exclude or set()
        async with self._lock:
            self._prune()
            workers = [
                dict(payload)
                for _, payload in self._workers.values()
                if payload.get("worker_id") not in excluded
                and payload.get("status") == "ACTIVE"
                and payload.get("profile_hash") == profile_hash
                and int(payload.get("running_sessions", 0)) < int(payload.get("capacity", 0))
                and (environment is None or payload.get("environment") == environment)
            ]
        if not workers:
            raise RuntimeError("NO_SANDBOX_WORKER_AVAILABLE")
        return min(
            workers,
            key=lambda item: (
                int(item.get("running_sessions", 0)) / max(int(item.get("capacity", 1)), 1)
            ),
        )

    async def runtime_snapshot(self) -> dict[str, Any]:
        """Read-only fleet capacity observation for the single-process registry."""
        async with self._lock:
            before = len(self._workers)
            self._prune()
            excluded = before - len(self._workers)
            entries = [payload for _, payload in self._workers.values()]
        truncated = len(entries) > _SNAPSHOT_MAX_WORKERS
        workers = [_snapshot_worker(payload) for payload in entries[:_SNAPSHOT_MAX_WORKERS]]
        return {
            "available": True,
            "scope": "observed_workers",
            "workers": workers,
            "excluded_workers": excluded,
            "truncated": truncated,
            "totals": _snapshot_totals(workers),
            "queue_length": None,
            "note": _SNAPSHOT_NOTE,
        }

    def _prune(self) -> None:
        now = time.monotonic()
        self._workers = {
            worker_id: entry for worker_id, entry in self._workers.items() if entry[0] > now
        }


def create_worker_registry(settings: Settings) -> Registry:
    if settings.registry_backend == "memory":
        return InMemoryWorkerRegistry(settings)
    if settings.registry_backend == "redis":
        from redis.asyncio import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        return WorkerRegistry(settings, client=client)
    return cast(
        "Registry",
        create_from_plugin(REGISTRY_GROUP, settings.registry_backend, settings),
    )


__all__ = [
    "InMemoryWorkerRegistry",
    "Registry",
    "WorkerRegistry",
    "create_worker_registry",
]
