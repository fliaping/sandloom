from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.registry import InMemoryWorkerRegistry
from agent_sandbox.sql_database import SqlAlchemyDatabase, normalize_async_database_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("sqlite:///control.db", "sqlite+aiosqlite"),
        ("mysql://user:pass@db/control", "mysql+aiomysql"),
        ("mysql+pymysql://user:pass@db/control", "mysql+aiomysql"),
        ("postgres://user:pass@db/control", "postgresql+asyncpg"),
        ("postgresql://user:pass@db/control", "postgresql+asyncpg"),
    ],
)
def test_database_urls_are_normalized_to_async_drivers(source: str, expected: str) -> None:
    assert normalize_async_database_url(source).drivername == expected


async def test_memory_registry_selects_live_compatible_worker(tmp_path: Path) -> None:
    settings = Settings(
        internal_token="token",
        local_root=tmp_path,
        heartbeat_ttl_seconds=30,
    )
    registry = InMemoryWorkerRegistry(settings)
    await registry.heartbeat(
        {
            "worker_id": "worker-a",
            "status": "ACTIVE",
            "profile_hash": "profile-a",
            "running_sessions": 1,
            "capacity": 4,
        }
    )
    await registry.heartbeat(
        {
            "worker_id": "worker-b",
            "status": "ACTIVE",
            "profile_hash": "profile-a",
            "running_sessions": 0,
            "capacity": 4,
        }
    )

    selected = await registry.select(profile_hash="profile-a")

    assert selected["worker_id"] == "worker-b"


async def test_sqlite_metadata_lifecycle(tmp_path: Path) -> None:
    pytest.importorskip("aiosqlite")
    settings = Settings(
        internal_token="token",
        local_root=tmp_path / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        profile_hash="profile-a",
    )
    database = SqlAlchemyDatabase(settings)
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
            sandbox_id="sandbox-a",
            workspace_scope_id="tenant-a/project-a",
            worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
        )
        assert route.generation == 1
        await database.mark_route_ready(
            route.sandbox_id,
            route.generation,
            "worker-a",
            "epoch-a",
        )
        assert (
            await database.begin_exec(
                sandbox_id=route.sandbox_id,
                exec_id="exec-a",
                generation=route.generation,
                worker_id="worker-a",
                command=json.dumps({"argv": ["/bin/true"]}),
            )
            is None
        )
        duplicate = await database.begin_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-a",
            generation=route.generation,
            worker_id="worker-a",
            command=json.dumps({"argv": ["/bin/true"]}),
        )
        assert duplicate is not None
        assert json.loads(duplicate["command_json"])["argv"] == ["/bin/true"]
        await database.finish_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-a",
            status="SUCCEEDED",
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
        )
        assert await database.begin_release(route.sandbox_id, route.generation) is True
        await database.release_route(
            route.sandbox_id,
            route.generation,
            reason="CLIENT_RELEASE",
            released_by="workspace:tenant-a/project-a",
        )

        released = await database.find_route(route.sandbox_id)
        assert released is not None
        assert released.status == "RELEASED"
        assert released.generation == 2
        assert released.last_release_reason == "CLIENT_RELEASE"
    finally:
        await database.close()
