"""Portable SQLAlchemy metadata store for PostgreSQL, MySQL, and SQLite."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    text,
    tuple_,
    update,
)
from sqlalchemy import inspect as schema_inspector
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .config import Settings
from .models import (
    Route,
    bounded_lifecycle_history,
    completed_lifecycle,
    route_from_mapping,
    utc_now_naive,
)

logger = logging.getLogger(__name__)

metadata = MetaData()

worker_table = Table(
    "agent_sandbox_worker",
    metadata,
    Column("worker_id", String(128), primary_key=True),
    Column("worker_epoch", String(64), nullable=False),
    Column("endpoint", String(512), nullable=False),
    Column("status", String(32), nullable=False),
    Column("capacity", Integer, nullable=False),
    Column("running_sessions", Integer, nullable=False, default=0),
    Column("profile_hash", String(191), nullable=False),
    Column("heartbeat_at", DateTime, nullable=False),
    Column("started_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)
Index("idx_agent_sandbox_worker_heartbeat", worker_table.c.status, worker_table.c.heartbeat_at)

route_table = Table(
    "agent_sandbox_route",
    metadata,
    Column("sandbox_id", String(128), primary_key=True),
    Column("workspace_scope_id", String(191), nullable=False),
    Column("worker_id", String(128)),
    Column("worker_epoch", String(64)),
    Column("generation", BigInteger, nullable=False, default=1),
    Column("sandbox_uid", Integer, nullable=False),
    Column("profile_id", String(64), nullable=False),
    Column("profile_hash", String(191), nullable=False),
    Column("status", String(32), nullable=False),
    Column("storage_mode", String(16), nullable=False),
    Column("active_exec_id", String(128)),
    Column("last_active_at", DateTime, nullable=False),
    Column("generation_started_at", DateTime, nullable=False),
    Column("generation_created_by", String(255)),
    Column("ready_at", DateTime),
    Column("last_released_generation", BigInteger),
    Column("last_released_at", DateTime),
    Column("last_release_reason", String(32)),
    Column("last_released_by", String(255)),
    Column("last_lifetime_ms", BigInteger),
    Column("lifecycle_count", Integer, nullable=False, default=1),
    Column("total_lifetime_ms", BigInteger, nullable=False, default=0),
    Column("lifecycle_history_json", JSON),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("sandbox_uid", name="uniq_agent_sandbox_uid"),
)
Index("idx_agent_sandbox_route_scope", route_table.c.workspace_scope_id)
Index("idx_agent_sandbox_route_worker", route_table.c.worker_id, route_table.c.status)

exec_table = Table(
    "agent_sandbox_exec",
    metadata,
    Column("sandbox_id", String(128), primary_key=True),
    Column("exec_id", String(128), primary_key=True),
    Column("generation", BigInteger, nullable=False),
    Column("worker_id", String(128), nullable=False),
    Column("status", String(32), nullable=False),
    Column("command_json", JSON, nullable=False),
    # Authoritative scope for mutual exclusion. It is a column rather than a
    # command_json field so admission never has to trust a caller's payload.
    Column("exec_scope", String(128)),
    Column("exit_code", Integer),
    Column("stdout_text", Text),
    Column("stderr_text", Text),
    Column("truncated", Boolean, nullable=False, default=False),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("created_at", DateTime, nullable=False),
)
Index("idx_agent_sandbox_exec_status", exec_table.c.sandbox_id, exec_table.c.status)


def normalize_async_database_url(value: str) -> URL:
    """Normalize common sync URLs to their supported async drivers."""
    url = make_url(value)
    driver = url.drivername
    replacements = {
        "mysql": "mysql+aiomysql",
        "mysql+pymysql": "mysql+aiomysql",
        "postgres": "postgresql+asyncpg",
        "postgresql": "postgresql+asyncpg",
        "postgresql+psycopg": "postgresql+asyncpg",
        "sqlite": "sqlite+aiosqlite",
    }
    return url.set(drivername=replacements.get(driver, driver))


# One prune statement's worth of rows. Small enough that the delete does not hold
# a write lock for a noticeable time, large enough that a backlog drains.
_EXEC_HISTORY_PRUNE_BATCH = 2000


class SqlAlchemyDatabase:
    """MetadataStore implementation shared by public SQL backends."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine: AsyncEngine | None = None

    async def connect(self) -> None:
        url = normalize_async_database_url(self.settings.resolved_database_url())
        if url.get_backend_name() == "sqlite" and url.database not in {None, "", ":memory:"}:
            await asyncio.to_thread(_ensure_sqlite_parent, url.database)
        options: dict[str, Any] = {"pool_pre_ping": True}
        if url.get_backend_name() != "sqlite":
            options["pool_recycle"] = self.settings.mysql_pool_recycle_seconds
        if url.get_backend_name() == "mysql":
            # aiomysql spells it `connect_timeout`, and without it the driver
            # waits on the OS default — which is how
            # `SANDBOX_MYSQL_CONNECT_TIMEOUT_SECONDS` came to be a setting that
            # changed nothing.
            options["connect_args"] = {
                "connect_timeout": self.settings.mysql_connect_timeout_seconds
            }
        self.engine = create_async_engine(url, **options)
        if url.get_backend_name() == "sqlite":
            _configure_sqlite(self.engine)
        try:
            auto_ddl = self.settings.database_auto_ddl
            if auto_ddl is None:
                auto_ddl = url.get_backend_name() == "sqlite"
            backend = url.get_backend_name()
            async with self.engine.begin() as connection:
                if auto_ddl:
                    # `create_all` checks for each object and then creates it, and
                    # two replicas starting together both see "not there yet" and
                    # both create. The loser gets a duplicate-key error from the
                    # system catalog and exits, so a rollout that starts replicas
                    # in parallel crash-loops until one of them has won the race.
                    # An advisory lock makes the check and the create one step.
                    async with advisory_lock(connection, backend, _DDL_LOCK_KEY):
                        await connection.run_sync(metadata.create_all)
                await _assert_schema_current(connection)
        except BaseException:
            # A half-connected store is worse than none: the engine holds a pool,
            # and a caller that retries would leave the old one behind with its
            # connections open. Nothing here survives the failure, so drop it.
            await self.engine.dispose()
            self.engine = None
            raise

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    def describe(self) -> dict[str, str]:
        url = normalize_async_database_url(self.settings.resolved_database_url())
        return {
            "backend": url.get_backend_name(),
            "database": url.database or "",
            "source": "url" if self.settings.database_url else "default-sqlite",
        }

    def _engine(self) -> AsyncEngine:
        if self.engine is None:
            raise RuntimeError("metadata database is not initialized")
        return self.engine

    async def upsert_worker(
        self, *, worker_id: str, epoch: str, endpoint: str, status: str, running: int
    ) -> None:
        now = utc_now_naive()
        values = {
            "worker_id": worker_id,
            "worker_epoch": epoch,
            "endpoint": endpoint,
            "status": status,
            "capacity": self.settings.worker_capacity,
            "running_sessions": running,
            "profile_hash": self.settings.profile_hash,
            "heartbeat_at": now,
            "started_at": now,
            "updated_at": now,
        }
        update_values = {
            key: value for key, value in values.items() if key not in {"worker_id", "started_at"}
        }
        try:
            async with self._engine().begin() as connection:
                result = await connection.execute(
                    update(worker_table)
                    .where(worker_table.c.worker_id == worker_id)
                    .values(**update_values)
                )
                if result.rowcount == 0:
                    await connection.execute(insert(worker_table).values(**values))
        except IntegrityError:
            # Another replica inserted the same stable worker_id after our
            # update. Retry as an update in a fresh transaction; the failed
            # transaction is not reusable on PostgreSQL.
            async with self._engine().begin() as connection:
                await connection.execute(
                    update(worker_table)
                    .where(worker_table.c.worker_id == worker_id)
                    .values(**update_values)
                )

    async def find_route(self, sandbox_id: str) -> Route | None:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(route_table).where(route_table.c.sandbox_id == sandbox_id)
            )
            row = result.mappings().first()
        return route_from_mapping(dict(row)) if row else None

    async def create_route(
        self, *, sandbox_id: str, workspace_scope_id: str, worker: dict[str, Any]
    ) -> Route:
        storage_mode = "shared" if self.settings.shared_root else "local"
        for _ in range(50):
            now = utc_now_naive()
            uid = random.randint(self.settings.uid_start, self.settings.uid_end)
            try:
                async with self._engine().begin() as connection:
                    await connection.execute(
                        insert(route_table).values(
                            sandbox_id=sandbox_id,
                            workspace_scope_id=workspace_scope_id,
                            worker_id=worker["worker_id"],
                            worker_epoch=worker["worker_epoch"],
                            generation=1,
                            sandbox_uid=uid,
                            profile_id=self.settings.profile_id,
                            profile_hash=self.settings.profile_hash,
                            status="ASSIGNED",
                            storage_mode=storage_mode,
                            last_active_at=now,
                            generation_started_at=now,
                            generation_created_by=f"workspace:{workspace_scope_id}",
                            lifecycle_count=1,
                            total_lifetime_ms=0,
                            lifecycle_history_json=[],
                            created_at=now,
                            updated_at=now,
                        )
                    )
            except IntegrityError:
                existing = await self.find_route(sandbox_id)
                if existing is not None:
                    return existing
                continue
            route = await self.find_route(sandbox_id)
            assert route is not None
            return route
        raise RuntimeError("Sandbox UID pool is exhausted")

    async def reassign_route(
        self,
        route: Route,
        worker: dict[str, Any],
        *,
        profile_hash: str | None = None,
        reason: str,
        created_by: str,
    ) -> Route:
        new_hash = profile_hash or route.profile_hash
        async with self._engine().begin() as connection:
            result = await connection.execute(
                select(route_table)
                .where(
                    route_table.c.sandbox_id == route.sandbox_id,
                    route_table.c.generation == route.generation,
                )
                .with_for_update()
            )
            row_mapping = result.mappings().first()
            if row_mapping is None:
                current = await self.find_route(route.sandbox_id)
                if current is None:
                    raise RuntimeError("Sandbox route disappeared during reassignment")
                return current
            row = dict(row_mapping)
            now = utc_now_naive()
            values = _reassignment_values(
                row,
                worker,
                profile_hash=new_hash,
                reason=reason,
                created_by=created_by,
                now=now,
            )
            updated = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == route.sandbox_id,
                    route_table.c.generation == route.generation,
                )
                .values(**values)
            )
            if updated.rowcount == 1:
                await connection.execute(
                    update(exec_table)
                    .where(
                        exec_table.c.sandbox_id == route.sandbox_id,
                        exec_table.c.generation == route.generation,
                        exec_table.c.status == "RUNNING",
                    )
                    .values(status="WORKER_LOST", finished_at=now)
                )
        current = await self.find_route(route.sandbox_id)
        if current is None:
            raise RuntimeError("Sandbox route disappeared during reassignment")
        return current

    async def mark_route_ready(
        self,
        sandbox_id: str,
        generation: int,
        worker_id: str,
        worker_epoch: str,
    ) -> None:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.worker_id == worker_id,
                    route_table.c.worker_epoch == worker_epoch,
                    route_table.c.status.in_(("ASSIGNED", "READY")),
                )
                .values(
                    status="READY",
                    last_active_at=now,
                    ready_at=func.coalesce(route_table.c.ready_at, now),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                raise RuntimeError("STALE_SANDBOX_ROUTE")

    async def touch_route(self, sandbox_id: str, generation: int) -> None:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    # A dormant route's last_active_at is its retention clock.
                    route_table.c.status.not_in(_INACTIVE_STATUSES),
                )
                .values(last_active_at=now, updated_at=now)
            )

    async def release_route(
        self,
        sandbox_id: str,
        generation: int,
        *,
        reason: str,
        released_by: str,
    ) -> None:
        async with self._engine().begin() as connection:
            result = await connection.execute(
                select(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status == "RELEASING",
                )
                .with_for_update()
            )
            row_mapping = result.mappings().first()
            if row_mapping is None:
                raise RuntimeError("STALE_SANDBOX_GENERATION")
            row = dict(row_mapping)
            now = utc_now_naive()
            event = completed_lifecycle(
                row, released_at=now, reason=reason, released_by=released_by
            )
            lifetime_ms = int(event["lifetime_ms"])
            history = bounded_lifecycle_history(
                row.get("lifecycle_history_json"), event=event, now=now
            )
            updated = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status == "RELEASING",
                )
                .values(
                    status="RELEASED",
                    active_exec_id=None,
                    last_released_generation=generation,
                    last_released_at=now,
                    last_release_reason=reason,
                    last_released_by=released_by,
                    last_lifetime_ms=lifetime_ms,
                    total_lifetime_ms=int(row.get("total_lifetime_ms") or 0) + lifetime_ms,
                    generation=generation + 1,
                    last_active_at=now,
                    lifecycle_history_json=history,
                    updated_at=now,
                )
            )
            if updated.rowcount != 1:
                raise RuntimeError("STALE_SANDBOX_GENERATION")

    async def prune_exec_history(self, *, older_than_days: int) -> int:
        """Delete executions that finished before the audit window.

        The row that records a command is also the row that holds its output, so
        this table is the size of a deployment's metadata store: 8,700 commands
        of test load left 84 MiB of stdout and stderr on disk, against 360 KiB
        for every route the deployment had ever known. Route lifecycle history
        was bounded from the start; this was not, and a store that only grows is
        one an operator has to reach into by hand.

        Only terminal executions are deleted, and the window is the one the audit
        trail already uses, so what a caller can still read is what the rest of
        the audit answers with. The batch is bounded because a single statement
        cannot hold the write lock for as long as a first prune of a large table
        would take; the sweep runs until a pass deletes nothing.
        """

        cutoff = utc_now_naive() - timedelta(days=older_than_days)
        async with self._engine().begin() as connection:
            keys = (
                (
                    await connection.execute(
                        select(exec_table.c.sandbox_id, exec_table.c.exec_id)
                        .where(
                            exec_table.c.status != "RUNNING",
                            func.coalesce(exec_table.c.finished_at, exec_table.c.started_at)
                            < cutoff,
                        )
                        .order_by(exec_table.c.started_at)
                        .limit(_EXEC_HISTORY_PRUNE_BATCH)
                    )
                )
                .mappings()
                .all()
            )
            if not keys:
                return 0
            result = await connection.execute(
                delete(exec_table).where(
                    tuple_(exec_table.c.sandbox_id, exec_table.c.exec_id).in_(
                        [(row["sandbox_id"], row["exec_id"]) for row in keys]
                    )
                )
            )
            return int(result.rowcount or 0)

    async def prune_lifecycle_history(self) -> int:
        changed = 0
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                select(route_table.c.sandbox_id, route_table.c.lifecycle_history_json)
                .where(route_table.c.lifecycle_history_json.is_not(None))
                .order_by(route_table.c.updated_at.asc())
                .limit(500)
                .with_for_update()
            )
            for row in result.mappings():
                original = row["lifecycle_history_json"] or []
                pruned = bounded_lifecycle_history(original, now=now)
                if pruned == original:
                    continue
                await connection.execute(
                    update(route_table)
                    .where(route_table.c.sandbox_id == row["sandbox_id"])
                    .values(lifecycle_history_json=pruned, updated_at=now)
                )
                changed += 1
        return changed

    async def begin_release(self, sandbox_id: str, generation: int) -> bool:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.active_exec_id.is_(None),
                    route_table.c.status.not_in(("RELEASED", "RELEASING")),
                )
                .values(status="RELEASING", last_active_at=now, updated_at=now)
            )
            if result.rowcount == 1:
                return True
        current = await self.find_route(sandbox_id)
        if current is None or current.generation != generation:
            raise RuntimeError("STALE_SANDBOX_GENERATION")
        if current.status == "RELEASED":
            return False
        if current.status == "RELEASING":
            return True
        raise RuntimeError("SANDBOX_BUSY_OR_STALE")

    async def list_reapable_routes(
        self,
        *,
        idle_ttl_seconds: int,
        releasing_grace_seconds: int,
        running_grace_seconds: int,
        limit: int,
    ) -> list[Route]:
        now = utc_now_naive()
        idle_cutoff = now - timedelta(seconds=idle_ttl_seconds)
        releasing_cutoff = now - timedelta(seconds=releasing_grace_seconds)
        running_cutoff = now - timedelta(seconds=running_grace_seconds)
        condition = or_(
            and_(
                route_table.c.active_exec_id.is_(None),
                or_(
                    and_(
                        route_table.c.status.in_(("ASSIGNED", "READY")),
                        route_table.c.last_active_at < idle_cutoff,
                    ),
                    and_(
                        route_table.c.status == "RELEASING",
                        route_table.c.updated_at < releasing_cutoff,
                    ),
                ),
            ),
            and_(
                route_table.c.status == "RUNNING",
                route_table.c.active_exec_id.is_not(None),
                route_table.c.last_active_at < running_cutoff,
            ),
        )
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(route_table)
                .where(condition)
                .order_by(route_table.c.last_active_at.asc())
                .limit(limit)
            )
            rows = result.mappings().all()
        return [route_from_mapping(dict(row)) for row in rows]

    async def begin_worker_lost_release(self, route: Route) -> bool:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == route.sandbox_id,
                    route_table.c.generation == route.generation,
                    route_table.c.worker_id == route.worker_id,
                    route_table.c.worker_epoch == route.worker_epoch,
                    route_table.c.status == "RUNNING",
                    route_table.c.active_exec_id.is_not(None),
                )
                .values(
                    status="RELEASING",
                    active_exec_id=None,
                    last_active_at=now,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                return False
            await connection.execute(
                update(exec_table)
                .where(
                    exec_table.c.sandbox_id == route.sandbox_id,
                    exec_table.c.generation == route.generation,
                    exec_table.c.status == "RUNNING",
                )
                .values(status="WORKER_LOST", finished_at=now)
            )
        return True

    async def begin_exec(
        self,
        *,
        sandbox_id: str,
        exec_id: str,
        generation: int,
        worker_id: str,
        command: str,
        exec_scope: str | None = None,
    ) -> dict[str, Any] | None:
        """Admit one execution, enforcing the parallel limit and scope exclusion.

        A scoped execution only conflicts with another execution in the same
        scope. An unscoped execution is lifecycle-level and stays exclusive, so
        it conflicts with everything currently running.
        """
        async with self._engine().begin() as connection:
            result = await connection.execute(
                select(exec_table).where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.exec_id == exec_id,
                )
            )
            existing = result.mappings().first()
            if existing:
                return _exec_row(dict(existing))
            # Serialize admission on the route row so two concurrent callers
            # cannot both observe capacity below the limit.
            route_result = await connection.execute(
                select(route_table.c.generation, route_table.c.status)
                .where(route_table.c.sandbox_id == sandbox_id)
                .with_for_update()
            )
            route = route_result.mappings().first()
            if (
                route is not None
                and int(route["generation"]) == generation
                and str(route["status"]) in _DORMANT_STATUSES
            ):
                # Admission and suspend both decide on this locked row, so an
                # exec either got in before the suspend or is refused here.
                raise RuntimeError("SANDBOX_SUSPENDED")
            if (
                route is None
                or int(route["generation"]) != generation
                or str(route["status"]) not in {"ASSIGNED", "READY", "RUNNING"}
            ):
                raise RuntimeError("STALE_SANDBOX_GENERATION")
            running_result = await connection.execute(
                select(exec_table.c.exec_id, exec_table.c.exec_scope, exec_table.c.command_json)
                .where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.generation == generation,
                    exec_table.c.status == "RUNNING",
                )
                .order_by(exec_table.c.started_at)
            )
            running = running_result.mappings().all()
            if len(running) >= self.settings.max_parallel_execs_per_sandbox:
                raise RuntimeError("SANDBOX_PARALLEL_EXEC_LIMIT")
            for running_row in running:
                running_scope = _running_scope(running_row)
                if exec_scope is None or running_scope is None or running_scope == exec_scope:
                    raise RuntimeError("SANDBOX_EXEC_SCOPE_BUSY")
            now = utc_now_naive()
            await connection.execute(
                insert(exec_table).values(
                    sandbox_id=sandbox_id,
                    exec_id=exec_id,
                    generation=generation,
                    worker_id=worker_id,
                    status="RUNNING",
                    command_json=json.loads(command),
                    exec_scope=exec_scope,
                    truncated=False,
                    started_at=now,
                    created_at=now,
                )
            )
            # active_exec_id keeps one representative execution for operators;
            # it is no longer an admission gate.
            admitted = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    # Re-checked in the write itself: SQLite has no FOR UPDATE,
                    # so the read above can predate a suspend or release that
                    # committed since. Zero rows rolls the insert back.
                    route_table.c.status.in_(("ASSIGNED", "READY", "RUNNING")),
                )
                .values(
                    active_exec_id=func.coalesce(route_table.c.active_exec_id, exec_id),
                    status="RUNNING",
                    last_active_at=now,
                    updated_at=now,
                )
            )
            if admitted.rowcount != 1:
                status_now = (
                    await connection.execute(
                        select(route_table.c.status).where(
                            route_table.c.sandbox_id == sandbox_id,
                            route_table.c.generation == generation,
                        )
                    )
                ).scalar()
                raise RuntimeError(
                    "SANDBOX_SUSPENDED"
                    if status_now in _DORMANT_STATUSES
                    else "STALE_SANDBOX_GENERATION"
                )
        return None

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
    ) -> None:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            exec_result = await connection.execute(
                select(exec_table.c.generation).where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.exec_id == exec_id,
                )
            )
            exec_row = exec_result.first()
            if exec_row is None:
                return
            generation = int(exec_row[0])
            # Serialize every completion on the route row, then pick the next
            # representative with a locking read. Otherwise two concurrent
            # completions can write an already finished exec back into
            # active_exec_id.
            route_result = await connection.execute(
                select(route_table.c.generation)
                .where(route_table.c.sandbox_id == sandbox_id)
                .with_for_update()
            )
            route_row = route_result.first()
            if route_row is None or int(route_row[0]) != generation:
                return
            await connection.execute(
                update(exec_table)
                .where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.exec_id == exec_id,
                )
                .values(
                    status=status,
                    exit_code=exit_code,
                    stdout_text=stdout,
                    stderr_text=stderr,
                    truncated=truncated,
                    finished_at=now,
                )
            )
            remaining_result = await connection.execute(
                select(exec_table.c.exec_id)
                .where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.generation == generation,
                    exec_table.c.status == "RUNNING",
                )
                .order_by(exec_table.c.started_at)
                .limit(1)
            )
            remaining = remaining_result.first()
            next_exec_id = str(remaining[0]) if remaining else None
            await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status.in_(("ASSIGNED", "READY", "RUNNING")),
                )
                .values(
                    active_exec_id=next_exec_id,
                    status="RUNNING" if next_exec_id else "READY",
                    last_active_at=now,
                    updated_at=now,
                )
            )

    async def get_exec(self, sandbox_id: str, exec_id: str) -> dict[str, Any] | None:
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(exec_table).where(
                    exec_table.c.sandbox_id == sandbox_id,
                    exec_table.c.exec_id == exec_id,
                )
            )
            row = result.mappings().first()
        return _exec_row(dict(row)) if row else None

    # ── suspend and resume ──
    #
    # READY -> SUSPENDING -> SUSPENDED -> ASSIGNED -> READY. Each step is a
    # conditional update on (sandbox_id, generation, status), so it either
    # happens exactly once or reports what happened instead.

    async def begin_suspend(self, sandbox_id: str, generation: int) -> bool:
        """Claim an idle route for suspension.

        True when this call claimed it, or a previous suspend is still in
        flight and should be finished; False when it is already suspended.
        Raises `SANDBOX_SUSPEND_BUSY` while a command runs, and
        `STALE_SANDBOX_GENERATION` when the caller's generation is not current.
        """
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            result = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.active_exec_id.is_(None),
                    route_table.c.status == "READY",
                )
                .values(status="SUSPENDING", updated_at=now)
            )
            if result.rowcount == 1:
                return True
        current = await self.find_route(sandbox_id)
        if current is None or current.generation != generation:
            raise RuntimeError("STALE_SANDBOX_GENERATION")
        if current.status == "SUSPENDED":
            return False
        if current.status == "SUSPENDING":
            return True
        if current.status == "RUNNING":
            raise RuntimeError("SANDBOX_SUSPEND_BUSY")
        if current.status == "ASSIGNED":
            # Resolved but never created: there is no slot to release yet.
            raise RuntimeError("SANDBOX_NOT_READY")
        raise RuntimeError("STALE_SANDBOX_GENERATION")

    async def finish_suspend(self, sandbox_id: str, generation: int) -> None:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status == "SUSPENDING",
                )
                # last_active_at starts the retention clock.
                .values(status="SUSPENDED", active_exec_id=None, last_active_at=now, updated_at=now)
            )
        current = await self.find_route(sandbox_id)
        if current is None or current.generation != generation or current.status != "SUSPENDED":
            raise RuntimeError("STALE_SANDBOX_GENERATION")

    async def abort_suspend(self, sandbox_id: str, generation: int) -> None:
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status == "SUSPENDING",
                )
                .values(status="READY", updated_at=now)
            )

    async def begin_resume(
        self,
        route: Route,
        worker: dict[str, Any],
        *,
        bump_generation: bool,
        profile_hash: str,
        created_by: str,
    ) -> Route | None:
        """Claim a suspended route for one worker, or return None if another call won.

        Without `bump_generation` the route stays on its worker at its
        generation, so a client holding that generation keeps using it. Moving
        to another worker, or to a restarted one, is a reassignment and bumps
        the generation exactly like any other, which fences the old owner.
        """
        async with self._engine().begin() as connection:
            result = await connection.execute(
                select(route_table)
                .where(
                    route_table.c.sandbox_id == route.sandbox_id,
                    route_table.c.generation == route.generation,
                    route_table.c.status == "SUSPENDED",
                )
                .with_for_update()
            )
            row_mapping = result.mappings().first()
            if row_mapping is None:
                return None
            now = utc_now_naive()
            if bump_generation:
                values = _reassignment_values(
                    dict(row_mapping),
                    worker,
                    profile_hash=profile_hash,
                    reason="RESUME_REASSIGNED",
                    created_by=created_by,
                    now=now,
                )
            else:
                values = {
                    "status": "ASSIGNED",
                    "worker_epoch": worker["worker_epoch"],
                    "last_active_at": now,
                    "updated_at": now,
                }
            updated = await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == route.sandbox_id,
                    route_table.c.generation == route.generation,
                    route_table.c.status == "SUSPENDED",
                )
                .values(**values)
            )
            if updated.rowcount != 1:
                return None
        return await self.find_route(route.sandbox_id)

    async def abort_resume(self, sandbox_id: str, generation: int) -> None:
        """Put a route whose resume failed back to sleep, where a retry can find it."""
        now = utc_now_naive()
        async with self._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(
                    route_table.c.sandbox_id == sandbox_id,
                    route_table.c.generation == generation,
                    route_table.c.status == "ASSIGNED",
                )
                .values(status="SUSPENDED", updated_at=now)
            )

    async def list_dormant_routes_to_reclaim(
        self,
        *,
        retention_seconds: int,
        suspending_grace_seconds: int,
        limit: int,
    ) -> list[Route]:
        """Suspended routes past retention, and suspends that stalled midway.

        `retention_seconds=0` keeps suspended routes indefinitely; stalled
        suspends are still returned so they can be finished.
        """
        now = utc_now_naive()
        conditions = [
            and_(
                route_table.c.status == "SUSPENDING",
                route_table.c.updated_at < now - timedelta(seconds=suspending_grace_seconds),
            )
        ]
        if retention_seconds > 0:
            conditions.append(
                and_(
                    route_table.c.status == "SUSPENDED",
                    route_table.c.last_active_at < now - timedelta(seconds=retention_seconds),
                )
            )
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(route_table)
                .where(or_(*conditions))
                .order_by(route_table.c.last_active_at.asc())
                .limit(limit)
            )
            rows = result.mappings().all()
        return [route_from_mapping(dict(row)) for row in rows]

    # ── fleet queries ──
    #
    # Every other read is keyed by a sandbox_id the caller already knows. An
    # operator does not: the first question is always "what is out there", so
    # these scan instead of looking up. They are deliberately paginated, since
    # a busy fleet holds far more routes than anyone wants in one response.

    async def list_routes(
        self,
        *,
        status: str | None = None,
        workspace_scope_id: str | None = None,
        worker_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Route], int]:
        """Page through routes, returning the page and the unpaged total."""
        conditions = []
        if status:
            conditions.append(route_table.c.status == status)
        if workspace_scope_id:
            conditions.append(route_table.c.workspace_scope_id == workspace_scope_id)
        if worker_id:
            conditions.append(route_table.c.worker_id == worker_id)
        if search:
            # Escape the LIKE wildcards themselves, or a `%` typed into the
            # console's search box would silently match every sandbox.
            pattern = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            conditions.append(route_table.c.sandbox_id.like(f"%{pattern}%", escape="\\"))
        query = select(route_table)
        counter = select(func.count()).select_from(route_table)
        if conditions:
            query = query.where(and_(*conditions))
            counter = counter.where(and_(*conditions))
        async with self._engine().connect() as connection:
            total = int((await connection.execute(counter)).scalar_one())
            result = await connection.execute(
                query.order_by(route_table.c.last_active_at.desc()).limit(limit).offset(offset)
            )
            rows = result.mappings().all()
        return [route_from_mapping(dict(row)) for row in rows], total

    async def count_routes_by_status(self) -> dict[str, int]:
        """Status histogram for the fleet, computed in the database."""
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(route_table.c.status, func.count()).group_by(route_table.c.status)
            )
            return {str(status): int(count) for status, count in result.all()}

    async def list_execs(
        self,
        *,
        sandbox_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """Page through execution history, newest first.

        stdout and stderr are excluded: a listing that inlined them would be
        enormous, and an operator scanning for a failure wants the exit code.
        Fetching one exec by id still returns the full output.
        """
        conditions = []
        if sandbox_id:
            conditions.append(exec_table.c.sandbox_id == sandbox_id)
        if status:
            conditions.append(exec_table.c.status == status)
        columns = [
            exec_table.c.sandbox_id,
            exec_table.c.exec_id,
            exec_table.c.generation,
            exec_table.c.worker_id,
            exec_table.c.status,
            exec_table.c.exec_scope,
            exec_table.c.command_json,
            exec_table.c.exit_code,
            exec_table.c.truncated,
            exec_table.c.started_at,
            exec_table.c.finished_at,
            exec_table.c.created_at,
        ]
        query = select(*columns)
        counter = select(func.count()).select_from(exec_table)
        if conditions:
            query = query.where(and_(*conditions))
            counter = counter.where(and_(*conditions))
        async with self._engine().connect() as connection:
            total = int((await connection.execute(counter)).scalar_one())
            result = await connection.execute(
                query.order_by(exec_table.c.created_at.desc()).limit(limit).offset(offset)
            )
            rows = [dict(row) for row in result.mappings().all()]
        for row in rows:
            row["command"] = _decode_command(row.pop("command_json"))
        return rows, total

    async def list_workers(self) -> list[dict[str, Any]]:
        """Every worker SQL knows about, including ones that stopped beating.

        The registry only reports live workers, so one that died is simply
        absent there — which is exactly the case an operator is investigating.
        """
        async with self._engine().connect() as connection:
            result = await connection.execute(
                select(worker_table).order_by(worker_table.c.worker_id.asc())
            )
            rows = result.mappings().all()
        return [dict(row) for row in rows]


_DORMANT_STATUSES = frozenset({"SUSPENDING", "SUSPENDED"})
_INACTIVE_STATUSES = ("RELEASED", "RELEASING", "SUSPENDING", "SUSPENDED")


def _reassignment_values(
    row: dict[str, Any],
    worker: dict[str, Any],
    *,
    profile_hash: str,
    reason: str,
    created_by: str,
    now: Any,
) -> dict[str, Any]:
    """The columns a reassignment writes: next generation, new owner, audit event."""
    generation = int(row["generation"])
    history = bounded_lifecycle_history(row.get("lifecycle_history_json"), now=now)
    values: dict[str, Any] = {
        "worker_id": worker["worker_id"],
        "worker_epoch": worker["worker_epoch"],
        "profile_hash": profile_hash,
        "generation": generation + 1,
        "status": "ASSIGNED",
        "active_exec_id": None,
        "last_active_at": now,
        "generation_started_at": now,
        "generation_created_by": created_by,
        "ready_at": None,
        "lifecycle_count": int(row.get("lifecycle_count") or 1) + 1,
        "updated_at": now,
    }
    if row["status"] != "RELEASED":
        event = completed_lifecycle(
            row,
            released_at=now,
            reason=reason,
            released_by="system:route-reassign",
        )
        history = bounded_lifecycle_history(history, event=event, now=now)
        lifetime_ms = int(event["lifetime_ms"])
        values.update(
            last_released_generation=generation,
            last_released_at=now,
            last_release_reason=reason,
            last_released_by="system:route-reassign",
            last_lifetime_ms=lifetime_ms,
            total_lifetime_ms=int(row.get("total_lifetime_ms") or 0) + lifetime_ms,
        )
    values["lifecycle_history_json"] = history
    return values


def database_backend_name(settings: Settings) -> str:
    return normalize_async_database_url(settings.resolved_database_url()).get_backend_name()


def _exec_row(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row.get("command_json"), str):
        row["command_json"] = json.dumps(
            row.get("command_json"), ensure_ascii=False, separators=(",", ":")
        )
    # The listing decodes the command into `command`; a row read one execution
    # at a time has to carry the same thing or the two views of one execution
    # disagree — which they did: the console could name the command in the list
    # and then show an empty one in the detail it opened.
    row["command"] = _decode_command(row["command_json"])
    return row


def _decode_command(value: Any) -> dict[str, Any]:
    """Return a command payload as a mapping, whatever the dialect handed back.

    A `JSON` column round-trips as a parsed object on some drivers and as raw
    text on others, and a listing is rendered directly by the console, so it
    normalizes here rather than making every caller guess.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _running_scope(row: Any) -> str | None:
    """Resolve a running execution's scope, preferring the authoritative column.

    Rows written before `exec_scope` became a column only carry the value inside
    `command_json`, so fall back to the payload during a rolling upgrade. A row
    that yields nothing is treated as unscoped, which is exclusive and therefore
    the safe answer.
    """
    scope = row["exec_scope"]
    if isinstance(scope, str) and scope:
        return scope
    return _exec_scope_of(row["command_json"])


def _exec_scope_of(payload: object) -> str | None:
    """Read exec_scope from a stored command payload.

    The column is JSON on PostgreSQL and MySQL but may come back as text or
    bytes from other drivers, so decode defensively. An unreadable payload is
    treated as unscoped, which is the exclusive and therefore safe answer.
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode(errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    scope = payload.get("exec_scope")
    return scope if isinstance(scope, str) else None


def _configure_sqlite(engine: AsyncEngine) -> None:
    """Make a SQLite deployment survive concurrent writers.

    Two settings, for two different failures.

    `journal_mode=WAL` is what stops readers and writers from excluding each
    other: in the default rollback-journal mode every write blocks every read
    for the length of its transaction, and a deployment running commands on 32
    threads at once starves a writer for seconds -- which arrives as
    `sqlite3.OperationalError: database is locked` and an HTTP 500 from the exec
    route, about one command in six hundred.

    `busy_timeout` is how long a writer waits for the lock instead of giving up.
    The DBAPI's five seconds is not enough on its own, and the reason is WAL's
    own housekeeping: a checkpoint runs inside a commit, copying the write-ahead
    log back into the database, and on a database with a history in it that is a
    multi-second commit. Fifteen seconds is chosen to outlast one, because the
    lock is always released and the alternative to waiting is failing a request
    that would have succeeded -- which is what a 500 from the exec route was, one
    command in six hundred, before any of this.

    `synchronous=NORMAL` is the WAL-appropriate durability choice: a commit no
    longer waits for an fsync of the journal, and a crash can lose the last
    transactions but cannot corrupt the database, which is the same guarantee
    this store already gives a fleet that can restart a worker.

    SQLite treats an unsupported journal mode as a request rather than an error:
    on a filesystem that cannot do WAL it stays in the mode it was, and the
    deployment keeps working with the contention it had before.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def _ensure_sqlite_parent(database: str) -> None:
    Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)


__all__ = [
    "SqlAlchemyDatabase",
    "database_backend_name",
    "exec_table",
    "metadata",
    "normalize_async_database_url",
    "route_table",
    "schema_drift",
    "worker_table",
]


# Arbitrary, fixed, and shared only by processes that create this schema.
_DDL_LOCK_KEY = 0x0A6E7_5A9D

_SCHEMA_EMPTY_HELP = (
    "None of this service's tables are in it. Apply "
    "`deploy/sql/generic-postgresql.sql` or `deploy/sql/generic-mysql.sql` before "
    "starting, or set `SANDBOX_DATABASE_AUTO_DDL=true` and let the service create "
    "them."
)

_SCHEMA_DRIFT_HELP = (
    "The database was created by an earlier version of this service. "
    "`deploy/sql/generic-postgresql.sql` and `deploy/sql/generic-mysql.sql` are "
    "full CREATE TABLE IF NOT EXISTS scripts, so re-applying one does not add a "
    "column to a table that already exists — the whole statement is skipped. "
    "Apply the missing change as an ALTER TABLE and start this version again."
)


def schema_drift(connection_sync: Any) -> tuple[list[str], list[str]]:
    """Compare the live schema with the one this build writes.

    `create_all` creates missing tables and indexes, and never alters a table
    that already exists, so a database from an earlier release keeps exactly the
    columns it had. The first request that touches a new column then fails with
    `no such column` — on a deployment whose test suite is green, which is a bad
    way to find out that a release needs a schema change. So the comparison runs
    at startup, where the answer is a crash-loop with a message that says what to
    do instead of a 500 on a request.

    Returns (missing, unexpected): objects this build needs that are absent, and
    objects this build does not write that are present.
    """

    inspector = schema_inspector(connection_sync)
    existing = set(inspector.get_table_names())
    missing: list[str] = []
    unexpected: list[str] = []
    for name, table in metadata.tables.items():
        if name not in existing:
            missing.append(name)
            continue
        have = {column["name"] for column in inspector.get_columns(name)}
        want = {column.name for column in table.columns}
        missing.extend(f"{name}.{column}" for column in sorted(want - have))
        unexpected.extend(f"{name}.{column}" for column in sorted(have - want))
    return missing, unexpected


async def _assert_schema_current(connection: AsyncConnection) -> None:
    missing, unexpected = await connection.run_sync(schema_drift)
    if unexpected:
        # A database ahead of the code is what a rollback looks like. The extra
        # columns are usually harmless, and refusing to start would make rolling
        # back impossible, so it is reported rather than fatal.
        logger.warning(
            "database has columns this build does not write: %s; a newer schema "
            "with an older binary is what a rollback looks like",
            ", ".join(unexpected),
        )
    if missing:
        # An empty database and one left by an earlier version need different
        # sentences: the help for drift says "the database was created by an
        # earlier version of this service", and telling an operator that about a
        # database nothing has ever been applied to sends them looking for a
        # previous deployment instead of at their own setup. Both were seen by
        # starting against a fresh database with auto-DDL off.
        existing = await connection.run_sync(
            lambda sync_connection: set(schema_inspector(sync_connection).get_table_names())
        )
        applied = bool(existing & set(metadata.tables))
        raise RuntimeError(
            "database schema is missing "
            + ", ".join(missing)
            + ". "
            + (_SCHEMA_DRIFT_HELP if applied else _SCHEMA_EMPTY_HELP)
        )


@contextlib.asynccontextmanager
async def advisory_lock(
    connection: AsyncConnection, backend: str, key: int, *, timeout_seconds: int = 60
) -> AsyncIterator[None]:
    """Hold a database-wide lock for the duration of a schema change.

    A no-op where the backend has no need of one: SQLite is a single file in a
    single process, and its `create_all` cannot race with a second replica that
    is configured to use it (`database_auto_ddl` is not implied for anything
    else). Requires an open transaction, because that is what bounds the lock.
    """

    if backend == "postgresql":
        # Transaction-scoped, so the commit is inside the critical section: the
        # lock is released when the transaction ends, not when this block does.
        # A session-scoped lock released by hand would be released *before* the
        # commit, and the second replica would then create the same tables from
        # outside the section, which is the failure this lock exists to prevent.
        # It would also be unreleasable after a failed statement: PostgreSQL
        # aborts the transaction, refuses every later statement, and the failed
        # unlock then replaces the duplicate-key error that caused it — which is
        # exactly how this came to be reported.
        await connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        yield
        return
    if backend == "mysql":
        acquired = (
            await connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": f"agent-sandbox-ddl-{key}", "timeout": timeout_seconds},
            )
        ).scalar()
        if acquired != 1:
            raise RuntimeError(
                "another replica is creating the schema and did not finish within "
                f"{timeout_seconds}s; retry once it has"
            )
        try:
            yield
        finally:
            # MySQL commits DDL implicitly, so the section is already durable.
            # The release must not be able to replace a failure from the body:
            # a lock that survives lives until the session ends, which is worth
            # a line but is not a better error than the one being reported.
            try:
                await connection.execute(
                    text("SELECT RELEASE_LOCK(:name)"), {"name": f"agent-sandbox-ddl-{key}"}
                )
            except Exception:
                logger.warning(
                    "could not release the schema lock %s; another replica may wait "
                    "on it until this connection closes",
                    key,
                    exc_info=True,
                )
        return
    yield
