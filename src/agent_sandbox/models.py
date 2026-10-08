"""Backend-neutral domain models and lifecycle audit helpers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

AUDIT_HISTORY_RETENTION_DAYS = 15
AUDIT_HISTORY_MAX_ENTRIES = 256
AUDIT_HISTORY_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class Route:
    sandbox_id: str
    workspace_scope_id: str
    worker_id: str | None
    worker_epoch: str | None
    generation: int
    sandbox_uid: int
    profile_id: str
    profile_hash: str
    status: str
    storage_mode: str
    last_active_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    generation_started_at: datetime | None = None
    generation_created_by: str | None = None
    ready_at: datetime | None = None
    last_released_generation: int | None = None
    last_released_at: datetime | None = None
    last_release_reason: str | None = None
    last_released_by: str | None = None
    last_lifetime_ms: int | None = None
    lifecycle_count: int = 1
    total_lifetime_ms: int = 0
    lifecycle_history: list[dict[str, Any]] = field(default_factory=list)


def route_from_mapping(row: Mapping[str, Any]) -> Route:
    """Create a route from a SQL-style mapping returned by an adapter."""
    return Route(
        sandbox_id=str(row["sandbox_id"]),
        workspace_scope_id=str(row["workspace_scope_id"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] else None,
        worker_epoch=str(row["worker_epoch"]) if row["worker_epoch"] else None,
        generation=int(row["generation"]),
        sandbox_uid=int(row["sandbox_uid"]),
        profile_id=str(row["profile_id"]),
        profile_hash=str(row["profile_hash"]),
        status=str(row["status"]),
        storage_mode=str(row["storage_mode"]),
        last_active_at=row.get("last_active_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        generation_started_at=row.get("generation_started_at") or row.get("created_at"),
        generation_created_by=(
            str(row["generation_created_by"])
            if row.get("generation_created_by")
            else f"workspace:{row['workspace_scope_id']}"
        ),
        ready_at=row.get("ready_at"),
        last_released_generation=(
            int(row["last_released_generation"])
            if row.get("last_released_generation") is not None
            else None
        ),
        last_released_at=row.get("last_released_at"),
        last_release_reason=(
            str(row["last_release_reason"]) if row.get("last_release_reason") else None
        ),
        last_released_by=(str(row["last_released_by"]) if row.get("last_released_by") else None),
        last_lifetime_ms=(
            int(row["last_lifetime_ms"]) if row.get("last_lifetime_ms") is not None else None
        ),
        lifecycle_count=int(row.get("lifecycle_count") or 1),
        total_lifetime_ms=int(row.get("total_lifetime_ms") or 0),
        lifecycle_history=bounded_lifecycle_history(row.get("lifecycle_history_json")),
    )


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return as_utc(value).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return as_utc(value)
    if not isinstance(value, str):
        return None
    try:
        return as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def parse_lifecycle_history(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    parsed: object = value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def serialize_lifecycle_history(history: list[dict[str, Any]]) -> str:
    return json.dumps(history, ensure_ascii=False, separators=(",", ":"))


def bounded_lifecycle_history(
    value: object,
    *,
    event: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keep recent events within both time and per-row JSON size limits."""
    current = as_utc(now or datetime.now(UTC))
    cutoff = current - timedelta(days=AUDIT_HISTORY_RETENTION_DAYS)
    history = [
        item
        for item in parse_lifecycle_history(value)
        if (released_at := parse_timestamp(item.get("released_at"))) is not None
        and released_at >= cutoff
    ]
    if event is not None:
        history.append(event)
    history = history[-AUDIT_HISTORY_MAX_ENTRIES:]
    while (
        history
        and len(serialize_lifecycle_history(history).encode("utf-8")) > AUDIT_HISTORY_MAX_BYTES
    ):
        history.pop(0)
    return history


def completed_lifecycle(
    row: Mapping[str, Any],
    *,
    released_at: datetime,
    reason: str,
    released_by: str,
) -> dict[str, Any]:
    started_at = row.get("ready_at") or row.get("generation_started_at") or row.get("created_at")
    normalized_started_at = as_utc(started_at) if isinstance(started_at, datetime) else None
    normalized_released_at = as_utc(released_at)
    lifetime_ms = (
        max(0, int((normalized_released_at - normalized_started_at).total_seconds() * 1000))
        if normalized_started_at is not None
        else 0
    )
    return {
        "generation": int(row["generation"]),
        "started_at": isoformat_utc(row.get("generation_started_at") or row.get("created_at")),
        "created_by": row.get("generation_created_by"),
        "ready_at": isoformat_utc(row.get("ready_at")),
        "released_at": isoformat_utc(released_at),
        "release_reason": reason,
        "released_by": released_by,
        "lifetime_ms": lifetime_ms,
    }


__all__ = [
    "AUDIT_HISTORY_MAX_BYTES",
    "AUDIT_HISTORY_MAX_ENTRIES",
    "AUDIT_HISTORY_RETENTION_DAYS",
    "Route",
    "bounded_lifecycle_history",
    "completed_lifecycle",
    "route_from_mapping",
    "serialize_lifecycle_history",
    "utc_now_naive",
]
