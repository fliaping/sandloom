"""Admission tests for scoped parallel execution against a real SQL backend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.sql_database import SqlAlchemyDatabase, route_table


def _command(scope: str | None) -> str:
    return json.dumps({"argv": ["/bin/true"], "exec_scope": scope})


async def _route_state(database: SqlAlchemyDatabase, sandbox_id: str) -> tuple[str | None, str]:
    """Read active_exec_id and status directly: Route does not expose the former."""
    from sqlalchemy import select

    async with database._engine().connect() as connection:
        result = await connection.execute(
            select(route_table.c.active_exec_id, route_table.c.status).where(
                route_table.c.sandbox_id == sandbox_id
            )
        )
        row = result.first()
    assert row is not None
    return row[0], str(row[1])


async def _ready_route(
    tmp_path: Path, *, max_parallel: int = 16
) -> tuple[SqlAlchemyDatabase, str, int]:
    pytest.importorskip("aiosqlite")
    settings = Settings(
        internal_token="token",
        local_root=tmp_path / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        profile_hash="profile-a",
        max_parallel_execs_per_sandbox=max_parallel,
    )
    database = SqlAlchemyDatabase(settings)
    await database.connect()
    await database.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )
    route = await database.create_route(
        sandbox_id="sandbox-a",
        workspace_scope_id="tenant-a/project-a",
        worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
    )
    await database.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
    return database, route.sandbox_id, route.generation


async def _begin(
    database: SqlAlchemyDatabase,
    sandbox_id: str,
    generation: int,
    exec_id: str,
    scope: str | None,
) -> dict[str, object] | None:
    return await database.begin_exec(
        sandbox_id=sandbox_id,
        exec_id=exec_id,
        generation=generation,
        worker_id="worker-a",
        command=_command(scope),
        exec_scope=scope,
    )


async def test_distinct_scopes_run_in_parallel(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        assert await _begin(database, sandbox_id, generation, "exec-1", "thread-1") is None
        assert await _begin(database, sandbox_id, generation, "exec-2", "thread-2") is None
        active_exec_id, status = await _route_state(database, sandbox_id)
        assert status == "RUNNING"
        # The first admitted execution stays the representative for operators.
        assert active_exec_id == "exec-1"
    finally:
        await database.close()


async def test_same_scope_is_rejected(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
            await _begin(database, sandbox_id, generation, "exec-2", "thread-1")
    finally:
        await database.close()


async def test_unscoped_execution_is_exclusive(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        # A lifecycle command must not interleave with scoped work.
        with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
            await _begin(database, sandbox_id, generation, "exec-2", None)
    finally:
        await database.close()


async def test_scoped_execution_waits_for_a_running_unscoped_command(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", None)
        with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_BUSY"):
            await _begin(database, sandbox_id, generation, "exec-2", "thread-1")
    finally:
        await database.close()


async def test_parallel_limit_is_enforced(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path, max_parallel=2)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        await _begin(database, sandbox_id, generation, "exec-2", "thread-2")
        with pytest.raises(RuntimeError, match="SANDBOX_PARALLEL_EXEC_LIMIT"):
            await _begin(database, sandbox_id, generation, "exec-3", "thread-3")
    finally:
        await database.close()


async def test_limit_is_checked_before_scope_conflict(tmp_path: Path) -> None:
    """A saturated sandbox reports capacity, which is retryable, not a conflict."""
    database, sandbox_id, generation = await _ready_route(tmp_path, max_parallel=1)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        with pytest.raises(RuntimeError, match="SANDBOX_PARALLEL_EXEC_LIMIT"):
            await _begin(database, sandbox_id, generation, "exec-2", "thread-1")
    finally:
        await database.close()


async def test_stale_generation_is_rejected(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
            await _begin(database, sandbox_id, generation + 1, "exec-1", "thread-1")
    finally:
        await database.close()


async def test_finish_promotes_a_remaining_execution(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        await _begin(database, sandbox_id, generation, "exec-2", "thread-2")
        await database.finish_exec(
            sandbox_id=sandbox_id,
            exec_id="exec-1",
            status="SUCCEEDED",
            exit_code=0,
            stdout="ok",
            stderr="",
            truncated=False,
        )
        # exec-1 finished, so the still-running exec-2 becomes representative.
        active_exec_id, status = await _route_state(database, sandbox_id)
        assert active_exec_id == "exec-2"
        assert status == "RUNNING"
    finally:
        await database.close()


async def test_finishing_the_last_execution_returns_the_route_to_ready(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        await database.finish_exec(
            sandbox_id=sandbox_id,
            exec_id="exec-1",
            status="SUCCEEDED",
            exit_code=0,
            stdout="ok",
            stderr="",
            truncated=False,
        )
        active_exec_id, status = await _route_state(database, sandbox_id)
        assert active_exec_id is None
        assert status == "READY"
    finally:
        await database.close()


async def test_finished_scope_can_be_reused(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        await database.finish_exec(
            sandbox_id=sandbox_id,
            exec_id="exec-1",
            status="SUCCEEDED",
            exit_code=0,
            stdout="ok",
            stderr="",
            truncated=False,
        )
        assert await _begin(database, sandbox_id, generation, "exec-2", "thread-1") is None
    finally:
        await database.close()


async def test_finish_is_idempotent_for_an_unknown_execution(tmp_path: Path) -> None:
    database, sandbox_id, _ = await _ready_route(tmp_path)
    try:
        await database.finish_exec(
            sandbox_id=sandbox_id,
            exec_id="exec-missing",
            status="SUCCEEDED",
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
        )
        route = await database.find_route(sandbox_id)
        assert route is not None
        assert route.status == "READY"
    finally:
        await database.close()


async def test_duplicate_exec_id_returns_the_existing_record(tmp_path: Path) -> None:
    database, sandbox_id, generation = await _ready_route(tmp_path)
    try:
        await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        # Retrying the same exec_id must be idempotent, not a scope conflict.
        existing = await _begin(database, sandbox_id, generation, "exec-1", "thread-1")
        assert existing is not None
        assert json.loads(existing["command_json"])["exec_scope"] == "thread-1"
    finally:
        await database.close()
