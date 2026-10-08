"""Stable storage interfaces and backend factories used by the control plane."""

from __future__ import annotations

from typing import Any, Protocol, cast

from .config import Settings
from .models import Route
from .plugins import METADATA_STORE_GROUP, create_from_plugin
from .sql_database import SqlAlchemyDatabase, database_backend_name


class MetadataStore(Protocol):
    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    def describe(self) -> dict[str, str]: ...
    async def find_route(self, sandbox_id: str) -> Route | None: ...
    async def create_route(
        self, *, sandbox_id: str, workspace_scope_id: str, worker: dict[str, Any]
    ) -> Route: ...
    async def upsert_worker(
        self, *, worker_id: str, epoch: str, endpoint: str, status: str, running: int
    ) -> None: ...
    async def reassign_route(
        self,
        route: Route,
        worker: dict[str, Any],
        *,
        profile_hash: str | None = None,
        reason: str,
        created_by: str,
    ) -> Route: ...
    async def mark_route_ready(
        self,
        sandbox_id: str,
        generation: int,
        worker_id: str,
        worker_epoch: str,
    ) -> None: ...
    async def touch_route(self, sandbox_id: str, generation: int) -> None: ...
    async def release_route(
        self,
        sandbox_id: str,
        generation: int,
        *,
        reason: str,
        released_by: str,
    ) -> None: ...
    async def prune_lifecycle_history(self) -> int: ...
    async def begin_release(self, sandbox_id: str, generation: int) -> bool: ...
    async def list_reapable_routes(
        self,
        *,
        idle_ttl_seconds: int,
        releasing_grace_seconds: int,
        running_grace_seconds: int,
        limit: int,
    ) -> list[Route]: ...
    async def begin_worker_lost_release(self, route: Route) -> bool: ...
    async def begin_exec(
        self,
        *,
        sandbox_id: str,
        exec_id: str,
        generation: int,
        worker_id: str,
        command: str,
        exec_scope: str | None = None,
    ) -> dict[str, Any] | None: ...
    async def finish_exec(
        self,
        *,
        sandbox_id: str,
        exec_id: str,
        status: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        truncated: bool,
    ) -> None: ...
    async def get_exec(self, sandbox_id: str, exec_id: str) -> dict[str, Any] | None: ...


class ExecHistoryPruning(Protocol):
    """Deleting recorded executions that have fallen out of the audit window.

    Separate from `MetadataStore` for the same reason `FleetQueries` is: a store
    written against an earlier release keeps working, and a store that keeps its
    records somewhere else -- an append-only log, a warehouse -- is not asked to
    delete from it. See `as_exec_history_pruning`.
    """

    async def prune_exec_history(self, *, older_than_days: int) -> int: ...


def as_exec_history_pruning(store: object) -> ExecHistoryPruning | None:
    """Narrow a metadata store to execution-history pruning, or report it cannot."""

    if store is None:
        return None
    if not callable(getattr(store, "prune_exec_history", None)):
        return None
    return cast("ExecHistoryPruning", store)


class FleetQueries(Protocol):
    """Fleet-wide reads used by the admin console.

    Kept separate from `MetadataStore` because these only answer "what exists",
    never participate in fencing, and an existing third-party store predates
    them. A store without these is not an error: the console degrades to the
    views it can serve. See `as_fleet_queries`.
    """

    async def list_routes(
        self,
        *,
        status: str | None = None,
        workspace_scope_id: str | None = None,
        worker_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Route], int]: ...
    async def count_routes_by_status(self) -> dict[str, int]: ...
    async def list_execs(
        self,
        *,
        sandbox_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]: ...
    async def list_workers(self) -> list[dict[str, Any]]: ...


def as_fleet_queries(store: object) -> FleetQueries | None:
    """Narrow a metadata store to the fleet-query API, or report it cannot.

    Returning `None` rather than raising keeps a custom metadata plugin working
    on an upgraded control plane; only the console views that need a scan are
    unavailable, and the API says so explicitly instead of returning empty
    results that look like an idle fleet.
    """
    if store is None:
        return None
    required = ("list_routes", "count_routes_by_status", "list_execs", "list_workers")
    if not all(callable(getattr(store, name, None)) for name in required):
        return None
    return cast("FleetQueries", store)


def create_metadata_store(settings: Settings) -> MetadataStore:
    """Select a metadata adapter without leaking backend details into services."""
    if settings.metadata_backend not in {"auto", "sqlalchemy"}:
        return cast(
            "MetadataStore",
            create_from_plugin(METADATA_STORE_GROUP, settings.metadata_backend, settings),
        )
    backend = database_backend_name(settings)
    if backend not in {"mysql", "postgresql", "sqlite"}:
        raise RuntimeError(f"unsupported metadata database backend: {backend}")
    return SqlAlchemyDatabase(settings)


__all__ = ["FleetQueries", "MetadataStore", "as_fleet_queries", "create_metadata_store"]
