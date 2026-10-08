"""What happens when the database is older than the binary.

`create_all` creates missing tables and never alters one that already exists, so
a database from an earlier release keeps the columns it had. Nothing else in the
project compares the two, which is how a deployment ends up with a green test
suite and a `no such column` on its first request.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from agent_sandbox.config import Settings
from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata, schema_drift


def _settings(database: Path, *, auto_ddl: bool) -> Settings:
    return Settings(
        internal_token="token",
        local_root=database.parent / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{database}",
        database_auto_ddl=auto_ddl,
        profile_hash="profile-a",
    )


def _statement(db: Path, sql: str) -> None:
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as connection:
        connection.execute(text(sql))
    engine.dispose()


async def _schema_of_this_build(tmp_path: Path) -> Path:
    """A database holding exactly what the current code writes."""
    db = tmp_path / "control.db"
    store = SqlAlchemyDatabase(_settings(db, auto_ddl=True))
    await store.connect()
    await store.close()
    return db


def test_drift_lists_missing_and_unexpected_columns(tmp_path: Path) -> None:
    """Both directions, by name, so the caller can act on either."""

    db = tmp_path / "control.db"
    engine = create_engine(f"sqlite:///{db}")
    metadata.create_all(engine)
    with engine.begin() as connection:
        assert schema_drift(connection) == ([], [])

        connection.execute(
            text("ALTER TABLE agent_sandbox_route DROP COLUMN lifecycle_history_json")
        )
        connection.execute(text("ALTER TABLE agent_sandbox_route ADD COLUMN from_the_future TEXT"))
        missing, unexpected = schema_drift(connection)

    engine.dispose()
    assert missing == ["agent_sandbox_route.lifecycle_history_json"]
    assert unexpected == ["agent_sandbox_route.from_the_future"]


async def test_a_database_this_build_created_is_accepted(tmp_path: Path) -> None:
    """The quick start creates its own schema; none of it counts as drift."""

    db = await _schema_of_this_build(tmp_path)
    store = SqlAlchemyDatabase(_settings(db, auto_ddl=False))
    await store.connect()
    await store.close()


async def test_a_missing_table_is_refused_by_name(tmp_path: Path) -> None:
    db = await _schema_of_this_build(tmp_path)
    _statement(db, "DROP TABLE agent_sandbox_exec")

    store = SqlAlchemyDatabase(_settings(db, auto_ddl=False))
    with pytest.raises(RuntimeError) as caught:
        await store.connect()
    await store.close()

    assert "agent_sandbox_exec" in str(caught.value)


async def test_a_missing_column_is_refused_and_says_what_to_do(tmp_path: Path) -> None:
    """The failure the README predicts, caught at startup instead of on a request."""

    db = await _schema_of_this_build(tmp_path)
    _statement(db, "ALTER TABLE agent_sandbox_route DROP COLUMN lifecycle_history_json")

    store = SqlAlchemyDatabase(_settings(db, auto_ddl=False))
    with pytest.raises(RuntimeError) as caught:
        await store.connect()
    await store.close()

    message = str(caught.value)
    assert "agent_sandbox_route.lifecycle_history_json" in message
    # `deploy/sql/*.sql` is CREATE TABLE IF NOT EXISTS, so "re-apply the schema"
    # is not the fix and must not be what the message implies.
    assert "ALTER TABLE" in message


async def test_an_empty_database_is_told_to_apply_the_schema(tmp_path: Path) -> None:
    """Nothing applied is not the same failure as something applied and stale.

    The column comparison cannot tell the two apart — in both cases every table
    is missing — so the first version of the message told an operator who had
    just created a database and started with auto-DDL off that it "was created
    by an earlier version of this service". There was no earlier version, and
    the sentence sends them looking for a previous deployment instead of at the
    DDL they have not run yet.
    """

    db = tmp_path / "control.db"
    _statement(db, "CREATE TABLE unrelated (id INTEGER)")

    store = SqlAlchemyDatabase(_settings(db, auto_ddl=False))
    with pytest.raises(RuntimeError) as caught:
        await store.connect()
    await store.close()

    message = str(caught.value)
    assert "agent_sandbox_route" in message
    assert "generic-postgresql.sql" in message
    assert "SANDBOX_DATABASE_AUTO_DDL=true" in message
    assert "earlier version" not in message


async def test_a_column_this_build_does_not_write_is_only_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A rollback has to stay possible, so an extra column is not fatal."""

    db = await _schema_of_this_build(tmp_path)
    _statement(db, "ALTER TABLE agent_sandbox_route ADD COLUMN from_the_future TEXT")

    store = SqlAlchemyDatabase(_settings(db, auto_ddl=False))
    with caplog.at_level(logging.WARNING, logger="agent_sandbox.sql_database"):
        await store.connect()
    await store.close()

    assert "agent_sandbox_route.from_the_future" in caplog.text


async def test_a_database_that_cannot_be_opened_fails_at_startup(tmp_path: Path) -> None:
    """The schema comparison forces `connect` to open the database.

    With auto-DDL off it returned without one, so a database that cannot be
    opened at all surfaced later, from the first write, as a driver error about
    something else.
    """

    store = SqlAlchemyDatabase(_settings(tmp_path, auto_ddl=False))
    with pytest.raises(SQLAlchemyError):
        await store.connect()
    # The failed attempt must not leave its pool attached: a caller that retries
    # would strand the old engine with its connections open.
    assert store.engine is None
    await store.close()
