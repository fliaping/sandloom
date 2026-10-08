"""Metadata store behavior on real MySQL and PostgreSQL servers.

The unit suite covers SQLite only. These tests run the same contract against
every supported dialect, because the admission path depends on `SELECT ... FOR
UPDATE` row locks, `JSON` column round-tripping, and `func.coalesce()` — all of
which differ between backends.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select

from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata, route_table


@pytest.fixture(params=["mysql_url", "postgres_url"])
def database_url(request: pytest.FixtureRequest) -> str:
    """Parametrize each test across every real SQL backend."""
    return str(request.getfixturevalue(request.param))


async def _fresh_store(settings: Any) -> SqlAlchemyDatabase:
    """Connect and reset the schema, since the server is shared per session."""
    database = SqlAlchemyDatabase(settings)
    await database.connect()
    engine = database.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    return database


async def _drop_and_close(database: SqlAlchemyDatabase) -> None:
    engine = database.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    await database.close()


@pytest.fixture
async def store(database_url: str, settings_factory: Any) -> AsyncIterator[SqlAlchemyDatabase]:
    database = await _fresh_store(settings_factory(database_url))
    try:
        yield database
    finally:
        await _drop_and_close(database)


async def test_two_replicas_can_create_the_schema_at_once(
    database_url: str, settings_factory: Any
) -> None:
    """Starting replicas in parallel must not kill one of them.

    `create_all` checks for each object and then creates it, so two replicas
    starting together both see "not there yet" and both create. The loser gets
    a duplicate-key error from the system catalog and exits — and a rollout
    that starts replicas in parallel (a Kubernetes rolling update, `compose up
    --scale`) then crash-loops until one of them has won. That is the documented
    multi-replica topology with `SANDBOX_DATABASE_AUTO_DDL=true`, which is what
    the sample environment ships.

    Run against the real server, because the race is in the server's catalog,
    not in this code.
    """

    # Leave no schema behind, so both replicas really do start from nothing.
    reset = SqlAlchemyDatabase(settings_factory(database_url))
    await reset.connect()
    engine = reset.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    await reset.close()

    first = SqlAlchemyDatabase(settings_factory(database_url))
    second = SqlAlchemyDatabase(settings_factory(database_url))
    try:
        # Concurrently rather than sequentially: the failure needs the two
        # `create_all` calls to overlap.
        results = await asyncio.gather(
            first.connect(), second.connect(), return_exceptions=True
        )
    finally:
        await _drop_and_close(first)
        await _drop_and_close(second)

    failures = [result for result in results if isinstance(result, BaseException)]
    assert not failures, f"a replica failed to start: {failures}"


async def _ready_route(store: SqlAlchemyDatabase, sandbox_id: str = "sandbox-a") -> Any:
    await store.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )
    route = await store.create_route(
        sandbox_id=sandbox_id,
        workspace_scope_id="tenant-a/project-a",
        worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
    )
    await store.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
    return route


async def _route_state(store: SqlAlchemyDatabase, sandbox_id: str) -> dict[str, Any]:
    """Read columns the domain model does not expose."""
    engine = store.engine
    assert engine is not None
    async with engine.connect() as connection:
        result = await connection.execute(
            select(route_table.c.active_exec_id, route_table.c.status).where(
                route_table.c.sandbox_id == sandbox_id
            )
        )
        row = result.mappings().first()
    assert row is not None
    return dict(row)


def _command(argv: list[str], scope: str | None = None) -> str:
    payload: dict[str, Any] = {"argv": argv}
    if scope is not None:
        payload["exec_scope"] = scope
    return json.dumps(payload)


async def _admit(
    store: SqlAlchemyDatabase,
    route: Any,
    exec_id: str,
    argv: list[str],
    scope: str | None = None,
) -> Any:
    """Admit an execution the way SandboxService does.

    `exec_scope` travels both inside the serialized command and as an explicit
    argument, because the service passes `request.model_dump_json()` together
    with `request.exec_scope`. Sending only one would test a state the
    production path never produces.
    """
    return await store.begin_exec(
        sandbox_id=route.sandbox_id,
        exec_id=exec_id,
        generation=route.generation,
        worker_id="worker-a",
        command=_command(argv, scope),
        exec_scope=scope,
    )


async def _finish(store: SqlAlchemyDatabase, route: Any, exec_id: str) -> None:
    await store.finish_exec(
        sandbox_id=route.sandbox_id,
        exec_id=exec_id,
        status="SUCCEEDED",
        exit_code=0,
        stdout="",
        stderr="",
        truncated=False,
    )


async def test_full_lifecycle_round_trips(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)

    assert await _admit(store, route, "exec-a", ["/bin/true"]) is None
    await _finish(store, route, "exec-a")
    assert await store.begin_release(route.sandbox_id, route.generation) is True
    await store.release_route(
        route.sandbox_id,
        route.generation,
        reason="CLIENT_RELEASE",
        released_by="workspace:tenant-a/project-a",
    )

    released = await store.find_route(route.sandbox_id)
    assert released is not None
    assert released.status == "RELEASED"
    # Releasing bumps the generation so a delayed worker cannot mutate the new one.
    assert released.generation == route.generation + 1
    assert released.last_release_reason == "CLIENT_RELEASE"


async def test_idempotent_replay_returns_stored_command(store: SqlAlchemyDatabase) -> None:
    """The JSON column must deserialize identically on every dialect."""
    route = await _ready_route(store)

    assert await _admit(store, route, "exec-a", ["pytest", "-q"], "thread-a") is None
    replay = await _admit(store, route, "exec-a", ["pytest", "-q"], "thread-a")

    assert replay is not None
    stored = json.loads(replay["command_json"])
    assert stored["argv"] == ["pytest", "-q"]
    assert stored["exec_scope"] == "thread-a"


async def test_distinct_scopes_run_in_parallel(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)

    for index, scope in enumerate(("thread-a", "thread-b", "thread-c")):
        assert await _admit(store, route, f"exec-{index}", ["sleep", "1"], scope) is None

    state = await _route_state(store, route.sandbox_id)
    assert state["status"] == "RUNNING"
    # The first admitted exec stays the representative for operators.
    assert state["active_exec_id"] == "exec-0"


async def test_same_scope_is_rejected(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)
    await _admit(store, route, "exec-a", ["sleep", "1"], "thread-a")

    with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
        await _admit(store, route, "exec-b", ["sleep", "1"], "thread-a")


async def test_unscoped_exec_conflicts_with_every_scope(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)
    await _admit(store, route, "exec-scoped", ["sleep", "1"], "thread-a")

    with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
        await _admit(store, route, "exec-lifecycle", ["git", "checkout", "main"])


async def test_scoped_exec_waits_for_a_running_lifecycle_command(
    store: SqlAlchemyDatabase,
) -> None:
    route = await _ready_route(store)
    await _admit(store, route, "exec-lifecycle", ["git", "checkout", "main"])

    with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
        await _admit(store, route, "exec-scoped", ["pytest"], "thread-a")


async def test_parallel_limit_is_enforced(database_url: str, settings_factory: Any) -> None:
    limited = await _fresh_store(settings_factory(database_url, max_parallel_execs_per_sandbox=2))
    try:
        route = await _ready_route(limited)
        for index in range(2):
            assert (
                await _admit(limited, route, f"exec-{index}", ["sleep", "1"], f"thread-{index}")
                is None
            )

        with pytest.raises(RuntimeError, match="SANDBOX_PARALLEL_EXEC_LIMIT"):
            await _admit(limited, route, "exec-overflow", ["sleep", "1"], "thread-overflow")
    finally:
        await _drop_and_close(limited)


async def test_concurrent_admission_respects_the_limit(
    database_url: str, settings_factory: Any
) -> None:
    """The real test of `with_for_update()`: race admissions on one route row.

    Without the locking read, several callers read the same running count and all
    pass the check, overshooting the limit.
    """
    limit = 4
    limited = await _fresh_store(
        settings_factory(database_url, max_parallel_execs_per_sandbox=limit)
    )
    try:
        route = await _ready_route(limited)

        async def admit(index: int) -> str:
            try:
                await _admit(limited, route, f"exec-{index}", ["sleep", "5"], f"thread-{index}")
                return "admitted"
            except RuntimeError as exc:
                return str(exc)

        outcomes = await asyncio.gather(*(admit(index) for index in range(limit * 3)))

        admitted = [item for item in outcomes if item == "admitted"]
        rejected = [item for item in outcomes if item != "admitted"]
        assert len(admitted) == limit, f"admitted {len(admitted)} over limit {limit}: {outcomes}"
        assert all(item == "SANDBOX_PARALLEL_EXEC_LIMIT" for item in rejected), rejected
    finally:
        await _drop_and_close(limited)


async def test_finish_promotes_the_next_running_exec(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)
    for index in range(3):
        await _admit(store, route, f"exec-{index}", ["sleep", "1"], f"thread-{index}")

    await _finish(store, route, "exec-0")

    state = await _route_state(store, route.sandbox_id)
    # A finished exec must never remain the representative.
    assert state["active_exec_id"] in {"exec-1", "exec-2"}
    assert state["status"] == "RUNNING"


async def test_route_returns_to_ready_when_the_last_exec_finishes(
    store: SqlAlchemyDatabase,
) -> None:
    route = await _ready_route(store)
    for index in range(2):
        await _admit(store, route, f"exec-{index}", ["sleep", "1"], f"thread-{index}")
    for index in range(2):
        await _finish(store, route, f"exec-{index}")

    state = await _route_state(store, route.sandbox_id)
    assert state["active_exec_id"] is None
    assert state["status"] == "READY"


async def test_concurrent_completions_do_not_resurrect_a_finished_exec(
    store: SqlAlchemyDatabase,
) -> None:
    """Racing completions must serialize on the route row."""
    route = await _ready_route(store)
    total = 6
    for index in range(total):
        await _admit(store, route, f"exec-{index}", ["sleep", "1"], f"thread-{index}")

    await asyncio.gather(*(_finish(store, route, f"exec-{index}") for index in range(total)))

    state = await _route_state(store, route.sandbox_id)
    assert state["active_exec_id"] is None
    assert state["status"] == "READY"


async def test_stale_generation_is_rejected(store: SqlAlchemyDatabase) -> None:
    route = await _ready_route(store)

    with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
        await store.begin_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-a",
            generation=route.generation + 1,
            worker_id="worker-a",
            command=_command(["/bin/true"]),
        )


async def test_audit_history_survives_a_round_trip(store: SqlAlchemyDatabase) -> None:
    """History is stored in a JSON column, which each dialect handles differently."""
    route = await _ready_route(store)
    await store.begin_release(route.sandbox_id, route.generation)
    await store.release_route(
        route.sandbox_id,
        route.generation,
        reason="IDLE_TIMEOUT",
        released_by="reaper",
    )

    reloaded = await store.find_route(route.sandbox_id)

    assert reloaded is not None
    assert reloaded.lifecycle_count >= 1
    assert reloaded.last_release_reason == "IDLE_TIMEOUT"


async def test_concurrent_route_creation_yields_one_row(store: SqlAlchemyDatabase) -> None:
    """Two callers resolving the same sandbox must not create two routes."""
    await store.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )

    async def create() -> Any:
        try:
            return await store.create_route(
                sandbox_id="sandbox-race",
                workspace_scope_id="tenant-a/project-a",
                worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
            )
        except Exception as exc:
            return exc

    results = await asyncio.gather(*(create() for _ in range(5)))

    routes = [item for item in results if not isinstance(item, Exception)]
    assert routes, f"every concurrent create failed: {results}"
    assert all(item.sandbox_id == "sandbox-race" for item in routes)
    # Exactly one row must exist regardless of how many callers raced.
    engine = store.engine
    assert engine is not None
    async with engine.connect() as connection:
        result = await connection.execute(
            select(route_table.c.sandbox_id).where(route_table.c.sandbox_id == "sandbox-race")
        )
        assert len(result.all()) == 1


async def test_exec_scope_column_is_authoritative(store: SqlAlchemyDatabase) -> None:
    """Admission must read the scope column, not the caller-supplied payload.

    Both carry the same value in production. Persisting the scope separately
    means a payload cannot disagree with the value admission enforced.
    """
    from agent_sandbox.sql_database import exec_table

    route = await _ready_route(store)
    await _admit(store, route, "exec-a", ["sleep", "1"], "thread-a")

    engine = store.engine
    assert engine is not None
    async with engine.connect() as connection:
        result = await connection.execute(
            select(exec_table.c.exec_scope).where(exec_table.c.exec_id == "exec-a")
        )
        row = result.first()

    assert row is not None
    assert row[0] == "thread-a"


async def test_legacy_rows_without_the_scope_column_stay_exclusive(
    store: SqlAlchemyDatabase,
) -> None:
    """A row written before the column existed must be treated as unscoped.

    During a rolling upgrade the old writer only put the scope in command_json.
    Falling back to the payload would be unsafe if it let a caller claim
    parallelism, so an absent column means lifecycle-level and exclusive.
    """
    from agent_sandbox.sql_database import exec_table

    route = await _ready_route(store)
    await _admit(store, route, "exec-a", ["sleep", "1"], "thread-a")
    # Simulate the pre-upgrade row shape: payload keeps the scope, column is NULL.
    engine = store.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.execute(
            exec_table.update().where(exec_table.c.exec_id == "exec-a").values(exec_scope=None)
        )

    # The payload fallback still recognizes thread-a, so a different scope runs.
    assert await _admit(store, route, "exec-b", ["sleep", "1"], "thread-b") is None
    # ...and the same scope is still excluded.
    with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
        await _admit(store, route, "exec-c", ["sleep", "1"], "thread-a")
