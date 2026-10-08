"""Running the shipped schema on a real server, and upgrading it afterwards.

`deploy/sql/generic-postgresql.sql` and `deploy/sql/generic-mysql.sql` are what an
operator applies by hand when the database is managed and autocommit-enabled DDL
is not wanted. The unit suite compares their text against the ORM, which says the
columns agree — not that the file is valid SQL for the server, nor that the
statement splitter an operator invents will survive it. These tests run the file.

The second half is the upgrade: a database created by an earlier release, and the
ALTER an operator has to apply to it before the new binary can use it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata

SQL_DIR = Path(__file__).resolve().parents[2] / "deploy" / "sql"

MISSING_COLUMN = "lifecycle_history_json"


def split_statements(script: str) -> list[str]:
    """Split a DDL script into statements, respecting quotes and comments.

    The files ship an inline `--` comment for one column, and the MySQL file
    comments a column with a string that contains a semicolon, so both a naive
    `split(";")` and a naive comment strip break them. An operator will hit this;
    the test should hit it first.
    """

    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        char = script[index]
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "-" and script.startswith("--", index):
            newline = script.find("\n", index)
            index = len(script) if newline == -1 else newline
            continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


async def _apply(engine: AsyncEngine, script: str) -> None:
    async with engine.begin() as connection:
        for statement in split_statements(script):
            await connection.execute(text(statement))


def _url_for(database_url: str) -> Path | None:
    """The shipped file that matches a database URL, or None if there is not one."""
    backend = database_url.split("+", 1)[0]
    name = {"postgresql": "generic-postgresql.sql", "mysql": "generic-mysql.sql"}.get(backend)
    return SQL_DIR / name if name else None


@pytest.fixture(params=["mysql_url", "postgres_url"])
def database_url(request: pytest.FixtureRequest) -> str:
    return str(request.getfixturevalue(request.param))


@pytest.fixture
async def shipped(
    database_url: str, settings_factory: Any
) -> AsyncIterator[tuple[SqlAlchemyDatabase, AsyncEngine]]:
    """A server whose schema came from the shipped file and nothing else."""
    script_path = _url_for(database_url)
    assert script_path is not None and script_path.exists(), script_path
    script = script_path.read_text()

    database = SqlAlchemyDatabase(settings_factory(database_url))
    await database.connect()
    engine = database.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
    # `create_all` would write the schema from ORM metadata, which is the thing
    # under test — the point is that the file alone is enough.
    await _apply(engine, script)
    try:
        yield database, engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await database.close()


async def test_the_shipped_file_is_the_whole_schema(
    shipped: tuple[SqlAlchemyDatabase, AsyncEngine], database_url: str, settings_factory: Any
) -> None:
    """The file alone has to satisfy a build that is forbidden from creating."""

    store = SqlAlchemyDatabase(settings_factory(database_url, database_auto_ddl=False))
    await store.connect()
    try:
        # Column-complete is not the same as usable: the store has to be able to
        # write and read a route through the schema the file created.
        await store.upsert_worker(
            worker_id="worker-sql",
            epoch="epoch-sql",
            endpoint="http://worker-sql:8080",
            status="ACTIVE",
            running=0,
        )
        route = await store.create_route(
            sandbox_id="sandbox-sql",
            workspace_scope_id="tenant/sql",
            worker={"worker_id": "worker-sql", "worker_epoch": "epoch-sql"},
        )
        assert route.generation == 1
    finally:
        await store.close()


async def test_a_database_from_an_earlier_release_is_refused_then_upgraded(
    shipped: tuple[SqlAlchemyDatabase, AsyncEngine], database_url: str, settings_factory: Any
) -> None:
    """The documented upgrade: an ALTER, because the file will not add a column.

    `CREATE TABLE IF NOT EXISTS` is skipped whole when the table exists, so
    re-applying the shipped file to a database created by an earlier release
    leaves it exactly as it was. The service has to say so at startup rather
    than fail on the first request that touches the column.
    """

    _, engine = shipped
    async with engine.begin() as connection:
        await connection.execute(
            text(f"ALTER TABLE agent_sandbox_route DROP COLUMN {MISSING_COLUMN}")
        )

    store = SqlAlchemyDatabase(settings_factory(database_url, database_auto_ddl=False))
    with pytest.raises(RuntimeError) as caught:
        await store.connect()
    message = str(caught.value)
    assert f"agent_sandbox_route.{MISSING_COLUMN}" in message

    # Re-applying the shipped file must not be what rescues it: that is the
    # mistake the message exists to prevent.
    script_path = _url_for(database_url)
    assert script_path is not None
    await _apply(engine, script_path.read_text())
    with pytest.raises(RuntimeError):
        await store.connect()

    async with engine.begin() as connection:
        await connection.execute(
            text(f"ALTER TABLE agent_sandbox_route ADD COLUMN {MISSING_COLUMN} JSON")
        )
    await store.connect()
    await store.close()


async def test_a_column_from_the_future_does_not_stop_a_rollback(
    shipped: tuple[SqlAlchemyDatabase, AsyncEngine], database_url: str, settings_factory: Any
) -> None:
    """An older binary against a newer database still starts."""

    _, engine = shipped
    async with engine.begin() as connection:
        await connection.execute(
            text("ALTER TABLE agent_sandbox_route ADD COLUMN from_the_future VARCHAR(32)")
        )

    store = SqlAlchemyDatabase(settings_factory(database_url, database_auto_ddl=False))
    await store.connect()
    await store.close()
