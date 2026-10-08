"""The lock that serializes schema creation between replicas.

Two replicas starting together both see a missing table and both create it; the
loser exits on a duplicate-key error from the system catalog, so a rolling update
crash-loops until one of them wins. The lock is what makes the check and the
create one step — which it only does if it spans the commit, and only if its own
failure handling cannot replace the error that caused it.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_sandbox.sql_database import advisory_lock

KEY = 0x0A6E7_5A9D
MYSQL_LOCK_NAME = f"agent-sandbox-ddl-{KEY}"


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar(self) -> Any:
        return self._value


class _Connection:
    """Records what was run, and fails where the caller says it should."""

    def __init__(self, *, acquire: Any = 1, fail_on: str | None = None) -> None:
        self.statements: list[str] = []
        self._acquire = acquire
        self._fail_on = fail_on

    async def execute(self, statement: Any, parameters: Any = None) -> _Result:
        sql = str(statement)
        self.statements.append(sql)
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError(f"{self._fail_on} failed")
        if "GET_LOCK" in sql:
            return _Result(self._acquire)
        return _Result(None)


async def test_postgresql_takes_the_transaction_scoped_lock() -> None:
    """A session-scoped lock is released before the commit it is meant to cover.

    `create_all` runs inside a transaction; `pg_advisory_unlock` released the lock
    while that transaction was still open, so a second replica could acquire it
    and create the same tables from outside the critical section. The
    transaction-scoped lock is released by the commit itself.
    """

    connection = _Connection()
    async with advisory_lock(connection, "postgresql", KEY):  # type: ignore[arg-type]
        pass

    assert connection.statements == ["SELECT pg_advisory_xact_lock(:key)"]
    # Nothing is issued on the way out: the transaction releases it.
    assert not any("unlock" in sql for sql in connection.statements)


async def test_mysql_takes_and_releases_the_lock() -> None:
    connection = _Connection()
    async with advisory_lock(connection, "mysql", KEY):  # type: ignore[arg-type]
        pass

    assert "GET_LOCK" in connection.statements[0]
    assert "RELEASE_LOCK" in connection.statements[-1]


async def test_mysql_refuses_to_wait_forever_for_another_replica() -> None:
    connection = _Connection(acquire=0)
    with pytest.raises(RuntimeError, match="another replica is creating the schema"):
        async with advisory_lock(connection, "mysql", KEY):  # type: ignore[arg-type]
            pass


async def test_a_failed_schema_change_is_not_reported_as_a_failed_unlock() -> None:
    """The real error has to survive the cleanup.

    This is not hypothetical: PostgreSQL aborts the transaction when a statement
    fails, so the unlock itself fails, and the duplicate-key error the lock
    exists to prevent was reported as a failure to unlock it.
    """

    connection = _Connection(fail_on="RELEASE_LOCK")
    with pytest.raises(ValueError, match="duplicate key value"):
        async with advisory_lock(connection, "mysql", KEY):  # type: ignore[arg-type]
            raise ValueError("duplicate key value violates unique constraint")


async def test_sqlite_takes_no_lock_at_all() -> None:
    """One file in one process: there is nobody to race with."""

    connection = _Connection()
    async with advisory_lock(connection, "sqlite", KEY):  # type: ignore[arg-type]
        pass

    assert connection.statements == []
