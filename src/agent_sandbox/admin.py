"""Read models for the admin console.

The operational APIs answer questions about a sandbox whose id the caller
already has. An operator starts without one: the first question is "what is
running, and where", and the second is "why is that one stuck". This module
assembles those fleet-wide views from the metadata store and the registry.

It is deliberately read-mostly. The only mutation exposed is releasing a
sandbox, which already exists as a supported lifecycle operation; the console
does not gain a privileged path that the API does not already have.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import Route
from .schemas import (
    AdminExecDetail,
    AdminExecSummary,
    AdminSandboxSummary,
    AdminWorkerSummary,
)

# A console page that asks for everything would turn one careless click into a
# full table scan, so the page size is capped here rather than trusted.
MAX_PAGE_SIZE = 200


def _aware(value: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC.

    Timestamps are stored naive-UTC, so comparing one against an aware `now`
    raises. Normalizing here keeps every age calculation in one place.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _age_seconds(value: datetime | None, *, now: datetime | None = None) -> float | None:
    moment = _aware(value)
    if moment is None:
        return None
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - moment).total_seconds())


def sandbox_summary(route: Route, *, now: datetime | None = None) -> AdminSandboxSummary:
    """Project a route onto the columns the console lists.

    `idle_seconds` is derived rather than stored because it is the field an
    operator actually sorts by when looking for something to reclaim, and a
    released sandbox has no meaningful idle time.
    """
    idle = None if route.status == "RELEASED" else _age_seconds(route.last_active_at, now=now)
    return AdminSandboxSummary(
        sandbox_id=route.sandbox_id,
        workspace_scope_id=route.workspace_scope_id,
        worker_id=route.worker_id,
        generation=route.generation,
        sandbox_uid=route.sandbox_uid,
        status=route.status,
        storage_mode=route.storage_mode,
        profile_id=route.profile_id,
        created_at=route.created_at,
        last_active_at=route.last_active_at,
        ready_at=route.ready_at,
        lifecycle_count=route.lifecycle_count,
        total_lifetime_ms=route.total_lifetime_ms,
        idle_seconds=idle,
    )


def exec_summary(row: dict[str, Any]) -> AdminExecSummary:
    """Project a stored execution onto its listing row.

    The duration is computed from the timestamps instead of read from a column,
    so a row that was written before the process finished still reports nothing
    rather than a misleading zero.
    """
    command = row.get("command") or {}
    argv = command.get("argv") if isinstance(command, dict) else None
    started = _aware(row.get("started_at"))
    finished = _aware(row.get("finished_at"))
    duration = None
    if started is not None and finished is not None:
        duration = max(0, int((finished - started).total_seconds() * 1000))
    return AdminExecSummary(
        sandbox_id=str(row["sandbox_id"]),
        exec_id=str(row["exec_id"]),
        generation=int(row["generation"]),
        worker_id=str(row["worker_id"]),
        status=str(row["status"]),
        exec_scope=row.get("exec_scope"),
        argv=[str(item) for item in argv] if isinstance(argv, list) else [],
        exit_code=row.get("exit_code"),
        truncated=bool(row.get("truncated")),
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        created_at=row.get("created_at"),
        duration_ms=duration,
    )


def worker_summaries(
    rows: list[dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    heartbeat_ttl_seconds: int,
    now: datetime | None = None,
) -> list[AdminWorkerSummary]:
    """Join persisted worker rows with live registry observations.

    SQL and the registry disagree by design: SQL keeps a row after a worker
    dies, while the registry drops it once the heartbeat expires. That
    disagreement is the useful signal, so both are reported rather than
    letting one silently win.
    """
    live_ids = {
        str(item.get("worker_id"))
        for item in snapshot.get("workers", [])
        if isinstance(item, dict) and item.get("worker_id")
    }
    summaries: list[AdminWorkerSummary] = []
    for row in rows:
        age = _age_seconds(row.get("heartbeat_at"), now=now)
        # Trust the registry when it lists the worker. When it does not, fall
        # back to the recorded heartbeat age, so a deployment using the
        # in-memory registry across replicas still shows something truthful.
        live = str(row.get("worker_id")) in live_ids or (
            age is not None and age < heartbeat_ttl_seconds
        )
        summaries.append(
            AdminWorkerSummary(
                worker_id=str(row["worker_id"]),
                worker_epoch=str(row.get("worker_epoch") or ""),
                endpoint=str(row.get("endpoint") or ""),
                status=str(row.get("status") or "UNKNOWN"),
                capacity=int(row.get("capacity") or 0),
                running_sessions=int(row.get("running_sessions") or 0),
                profile_hash=str(row.get("profile_hash") or ""),
                started_at=row.get("started_at"),
                heartbeat_at=row.get("heartbeat_at"),
                heartbeat_age_seconds=age,
                live=live,
            )
        )
    return summaries


def exec_detail(row: dict[str, Any]) -> AdminExecDetail:
    """A recorded execution with its output, from the stored row."""
    return AdminExecDetail(
        **exec_summary(row).model_dump(),
        stdout=str(row.get("stdout_text") or ""),
        stderr=str(row.get("stderr_text") or ""),
    )


def clamp_page(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(limit, MAX_PAGE_SIZE)), max(0, offset)


__all__ = [
    "MAX_PAGE_SIZE",
    "clamp_page",
    "exec_detail",
    "exec_summary",
    "sandbox_summary",
    "worker_summaries",
]
