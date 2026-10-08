"""Portable configuration tests."""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_sandbox.config import Settings, get_environment, get_environment_type
from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("environment", ["local", "staging", "prod"])
def test_environment_is_explicit(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", environment)
    assert get_environment() == environment


def test_unknown_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "preview")

    with pytest.raises(RuntimeError, match="SANDBOX_ENVIRONMENT"):
        get_environment()


def test_environment_type_uses_generic_zone_and_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "prod")
    monkeypatch.setenv("SANDBOX_ZONE", "zone-a")
    monkeypatch.setenv("SANDBOX_REGION", "region-1")

    assert get_environment_type() == "PROD-zone-a-region-1"


def test_default_database_is_embedded_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_DATABASE_URL", raising=False)
    assert Settings(database_url="").resolved_database_url().startswith("sqlite+aiosqlite:")


def test_explicit_database_url_wins() -> None:
    url = "postgresql+asyncpg://user:password@db.example/agent_sandbox"
    assert Settings(database_url=url).resolved_database_url() == url


def test_blobstore_prefix_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOBSTORE_BASE_PREFIX", "/tenant/checkpoints/")
    assert Settings().blobstore_base_prefix == "tenant/checkpoints/"


def test_private_backend_names_are_accepted_and_normalized() -> None:
    settings = Settings(
        metadata_backend="Company-Metadata",
        registry_backend="Company-Registry",
        object_store_backend="Company-Objects",
        execution_backend="Company-Runtime",
    )

    assert settings.metadata_backend == "company-metadata"
    assert settings.registry_backend == "company-registry"
    assert settings.object_store_backend == "company-objects"
    assert settings.execution_backend == "company-runtime"


_NON_COLUMN = ("PRIMARY KEY", "UNIQUE", "CONSTRAINT", "FOREIGN KEY", "KEY ", "INDEX")


def _columns_per_table(sql: str) -> dict[str, set[str]]:
    """Every table in a schema file, with the columns it declares."""

    tables: dict[str, set[str]] = {}
    for statement in re.finditer(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\((.*?)\n\)", sql, re.S
    ):
        name, body = statement.group(1), statement.group(2)
        columns = set()
        for line in body.splitlines():
            line = re.sub(r"COMMENT\s+'.*?'", "", re.sub(r"--.*$", "", line)).strip()
            if not line or line.startswith(_NON_COLUMN):
                continue
            columns.add(line.split()[0].strip('`"'))
        tables[name] = columns
    return tables


@pytest.mark.parametrize("filename", ["generic-postgresql.sql", "generic-mysql.sql"])
def test_the_shipped_schema_matches_what_the_code_writes(filename: str) -> None:
    """`deploy/sql` is applied by hand when auto-DDL is off, and it cannot drift.

    A deployment that turns `SANDBOX_DATABASE_AUTO_DDL` off — which is what a
    managed database expects, and what both adapter documents point at — gets
    its tables from one of these files. Drift shows up as `no such column` on
    the first request that touches the new one, in production, with every test
    still green: the suite creates its schema with `create_all` and never reads
    this file. Both dialects were also run end to end this way.
    """

    sql = (ROOT / "deploy" / "sql" / filename).read_text(encoding="utf-8")
    shipped = _columns_per_table(sql)

    assert set(shipped) == set(metadata.tables), (
        f"{filename} and the code disagree about which tables exist: "
        f"{sorted(set(shipped) ^ set(metadata.tables))}"
    )
    for name, table in sorted(metadata.tables.items()):
        declared = {column.name for column in table.columns}
        assert shipped[name] == declared, (
            f"{filename}.{name} disagrees with the table the code writes; "
            f"only in the code: {sorted(declared - shipped[name])}, "
            f"only in the file: {sorted(shipped[name] - declared)}"
        )


def _sqlite_settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path / "sandboxes",
        min_free_bytes=0,
        advertise_host="worker.test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
    )


async def test_sqlite_runs_in_wal_with_a_timeout_that_outlasts_a_checkpoint(
    tmp_path: Path,
) -> None:
    """The two pragmas a single-node deployment needs to survive its own load.

    Both were missing, and the cost was measurable rather than theoretical:
    at 32 concurrent commands the documented load test answered HTTP 500 from
    the exec route about once per six hundred commands, every one of them
    `sqlite3.OperationalError: database is locked` on a write that had waited the
    DBAPI's five seconds. The same run after the pragmas: no failures, and 31
    commands per second instead of 21.
    """

    database = SqlAlchemyDatabase(_sqlite_settings(tmp_path))
    await database.connect()
    try:
        assert database.engine is not None
        async with database.engine.connect() as connection:
            journal = (await connection.exec_driver_sql("PRAGMA journal_mode")).scalar()
            timeout = (await connection.exec_driver_sql("PRAGMA busy_timeout")).scalar()

        assert str(journal).lower() == "wal", (
            "without WAL every write blocks every read, which is how a writer "
            "starves under a concurrency the deployment advertises"
        )
        assert int(timeout) >= 15000, (
            f"a {timeout}ms wait cannot outlast a checkpoint on a database with "
            "history in it, and a writer that gives up is a 500"
        )
    finally:
        await database.close()


async def test_a_writer_waits_for_another_transaction_instead_of_failing(
    tmp_path: Path,
) -> None:
    """The behavior those pragmas buy, asserted rather than described.

    A second connection holds the write lock and releases it after half a
    second. A store that does not wait answers immediately with
    `database is locked`, which is the failure a caller sees as a 500.
    """

    settings = _sqlite_settings(tmp_path)
    database = SqlAlchemyDatabase(settings)
    await database.connect()
    # `check_same_thread=False` so the timer may release it: sqlite3 refuses a
    # connection used from a thread other than the one that opened it.
    blocker = sqlite3.connect(str(tmp_path / "control.db"), check_same_thread=False)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute("UPDATE agent_sandbox_worker SET status = 'ACTIVE' WHERE 1 = 0")
        timer = threading.Timer(0.5, blocker.rollback)
        timer.start()

        await database.upsert_worker(
            worker_id="w-1",
            epoch="e-1",
            endpoint="http://w-1:8080",
            status="ACTIVE",
            running=0,
        )
        timer.cancel()
    finally:
        blocker.close()
        await database.close()

    # And it committed, rather than merely not raising.
    check = sqlite3.connect(str(tmp_path / "control.db"))
    try:
        row = check.execute(
            "SELECT status FROM agent_sandbox_worker WHERE worker_id = 'w-1'"
        ).fetchone()
    finally:
        check.close()

    assert row == ("ACTIVE",)
