from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from agent_sandbox.models import (
    AUDIT_HISTORY_MAX_BYTES,
    AUDIT_HISTORY_MAX_ENTRIES,
    AUDIT_HISTORY_RETENTION_DAYS,
    Route,
    bounded_lifecycle_history,
    completed_lifecycle,
    serialize_lifecycle_history,
)
from agent_sandbox.service import route_audit


def test_route_audit_reports_current_and_last_lifecycle() -> None:
    now = datetime.now(UTC)
    route = Route(
        sandbox_id="audit-123",
        workspace_scope_id="scope-123",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-v9",
        status="READY",
        storage_mode="shared",
        last_active_at=now,
        created_at=now - timedelta(minutes=20),
        generation_started_at=now - timedelta(minutes=5),
        generation_created_by="workspace:scope-123",
        ready_at=now - timedelta(minutes=4),
        last_released_generation=1,
        last_released_at=now - timedelta(minutes=6),
        last_release_reason="IDLE_TIMEOUT",
        last_released_by="system:orphan-reaper",
        last_lifetime_ms=60000,
        lifecycle_count=2,
        total_lifetime_ms=60000,
        lifecycle_history=[
            {
                "generation": 1,
                "started_at": (now - timedelta(minutes=8)).isoformat(),
                "created_by": "workspace:scope-123",
                "ready_at": (now - timedelta(minutes=7)).isoformat(),
                "released_at": (now - timedelta(minutes=6)).isoformat(),
                "release_reason": "IDLE_TIMEOUT",
                "released_by": "system:orphan-reaper",
                "lifetime_ms": 60000,
            }
        ],
    )

    audit = route_audit(route)

    assert audit.generation_created_by == "workspace:scope-123"
    assert audit.last_release_reason == "IDLE_TIMEOUT"
    assert audit.last_lifetime_ms == 60000
    assert audit.history_retention_days == 15
    assert audit.history_max_entries == 256
    assert audit.history_max_bytes == 65536
    assert [entry.generation for entry in audit.history] == [1]
    assert 239000 <= (audit.active_duration_ms or 0) <= 241000


def test_lifecycle_history_keeps_only_recent_bounded_entries() -> None:
    now = datetime.now(UTC)
    recent = [
        {
            "generation": generation,
            "started_at": (now - timedelta(minutes=2)).isoformat(),
            "created_by": "workspace:scope-123",
            "ready_at": (now - timedelta(minutes=1)).isoformat(),
            "released_at": now.isoformat(),
            "release_reason": "CLIENT_RELEASE",
            "released_by": "workspace:scope-123",
            "lifetime_ms": 60000,
        }
        for generation in range(1, 400)
    ]
    expired = {
        **recent[0],
        "generation": 0,
        "released_at": (now - timedelta(days=16)).isoformat(),
    }

    history = bounded_lifecycle_history([expired, *recent], now=now)

    assert all(entry["generation"] != 0 for entry in history)
    assert len(history) <= AUDIT_HISTORY_MAX_ENTRIES
    assert len(serialize_lifecycle_history(history).encode()) <= AUDIT_HISTORY_MAX_BYTES
    assert history[-1]["generation"] == 399


def test_same_sandbox_multiple_restores_keep_each_completed_generation() -> None:
    now = datetime.now(UTC)
    history: list[dict[str, object]] = []
    for offset, generation in enumerate((1, 3, 5), start=1):
        released_at = now - timedelta(minutes=4 - offset)
        event = completed_lifecycle(
            {
                "generation": generation,
                "generation_started_at": released_at - timedelta(minutes=2),
                "generation_created_by": "workspace:scope-123",
                "ready_at": released_at - timedelta(minutes=1),
            },
            released_at=released_at,
            reason="CLIENT_RELEASE",
            released_by="workspace:scope-123",
        )
        history = bounded_lifecycle_history(history, event=event, now=now)

    assert [entry["generation"] for entry in history] == [1, 3, 5]
    assert [entry["lifetime_ms"] for entry in history] == [60000, 60000, 60000]


async def test_execution_history_is_pruned_to_the_audit_window(tmp_path: Path) -> None:
    """Only the past, and only what finished.

    The record of a command is also the record of its output, so this is the
    table a deployment actually grows: 8,700 commands of load left 84 MiB of
    stdout and stderr against 360 KiB of routes, and nothing was deleting any of
    it. What is deleted has to be exactly the rows that fell out of the window
    the rest of the audit already uses -- a command still running owns a row a
    caller may still be polling, and a recent one is the audit.
    """
    from agent_sandbox.config import Settings
    from agent_sandbox.sql_database import SqlAlchemyDatabase

    database = SqlAlchemyDatabase(
        Settings(
            internal_token="token",
            local_root=tmp_path / "sandboxes",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
            database_auto_ddl=True,
            advertise_host="worker.test",
        )
    )
    await database.connect()
    try:
        await database.upsert_worker(
            worker_id="worker-a",
            epoch="epoch-a",
            endpoint="http://worker-a:8080",
            status="ACTIVE",
            running=0,
        )
        route = await database.create_route(
            sandbox_id="sb-old",
            workspace_scope_id="tenant/a",
            worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
        )
        for exec_id in ("ancient", "recent", "still-running"):
            await database.begin_exec(
                sandbox_id=route.sandbox_id,
                exec_id=exec_id,
                generation=route.generation,
                worker_id="worker-a",
                command='{"argv": ["/bin/true"]}',
                # One scope each: an unscoped execution is lifecycle-level and
                # exclusive, so a second would be refused with
                # SANDBOX_EXEC_SCOPE_BUSY rather than recorded.
                exec_scope=f"probe-{exec_id}",
            )
        for exec_id in ("ancient", "recent"):
            await database.finish_exec(
                sandbox_id=route.sandbox_id,
                exec_id=exec_id,
                status="SUCCEEDED",
                exit_code=0,
                stdout="x",
                stderr="",
                truncated=False,
            )

        # Backdate two of them past the window, through the columns the prune
        # reads rather than through a fixture that could drift from them. Both
        # are old; only one has finished, which is the whole difference the
        # prune is allowed to act on. A long-running command is old without
        # being over: its row is what its caller is polling.
        old = datetime.now(UTC) - timedelta(days=AUDIT_HISTORY_RETENTION_DAYS + 5)
        async with database.engine.begin() as connection:  # type: ignore[union-attr]
            await connection.execute(
                text(
                    "UPDATE agent_sandbox_exec SET started_at = :old, finished_at = :old "
                    "WHERE exec_id = 'ancient'"
                ),
                # As the string the column stores, rather than a datetime the
                # DBAPI would adapt with a deprecated default.
                {"old": old.strftime("%Y-%m-%d %H:%M:%S.%f")},
            )
            await connection.execute(
                text(
                    "UPDATE agent_sandbox_exec SET started_at = :old "
                    "WHERE exec_id = 'still-running'"
                ),
                {"old": old.strftime("%Y-%m-%d %H:%M:%S.%f")},
            )

        removed = await database.prune_exec_history(older_than_days=AUDIT_HISTORY_RETENTION_DAYS)

        assert removed == 1
        assert await database.get_exec(route.sandbox_id, "ancient") is None
        recent = await database.get_exec(route.sandbox_id, "recent")
        running = await database.get_exec(route.sandbox_id, "still-running")
        assert recent is not None and recent["status"] == "SUCCEEDED"
        assert running is not None and running["status"] == "RUNNING"

        # Nothing left to do is reported as nothing left to do, which is what
        # stops the sweep rather than making it loop.
        assert await database.prune_exec_history(older_than_days=AUDIT_HISTORY_RETENTION_DAYS) == 0
    finally:
        await database.close()
