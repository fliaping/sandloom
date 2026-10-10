"""Suspend and resume on the middleware the project ships adapters for.

The unit suite proves the `DormantRouteStore` transitions on SQLite and the
snapshot archive against a directory. These run the same contract on real
MySQL and PostgreSQL servers, where exec admission and suspend race on a
`SELECT ... FOR UPDATE` row lock instead of SQLite's single writer, and the
snapshot against a real S3 API, where deletes and listings behave like the
service they stand in for.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from agent_sandbox.archive import ArchiveLimits
from agent_sandbox.blobstore import BlobStore
from agent_sandbox.dormant import (
    delete_snapshot,
    manifest_key,
    read_manifest,
    restore_snapshot,
    write_snapshot,
)
from agent_sandbox.models import Route, utc_now_naive
from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata, route_table
from agent_sandbox.storage import as_dormant_route_store

BUCKET = "agent-sandbox-integration"
WORKER_A = {"worker_id": "worker-a", "worker_epoch": "epoch-a"}
WORKER_B = {"worker_id": "worker-b", "worker_epoch": "epoch-b"}


@pytest.fixture(params=["mysql_url", "postgres_url"])
def database_url(request: pytest.FixtureRequest) -> str:
    return str(request.getfixturevalue(request.param))


@pytest.fixture
async def store(database_url: str, settings_factory: Any) -> AsyncIterator[SqlAlchemyDatabase]:
    database = SqlAlchemyDatabase(settings_factory(database_url))
    await database.connect()
    engine = database.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    try:
        yield database
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await database.close()


async def _ready(store: SqlAlchemyDatabase, sandbox_id: str = "sandbox-a") -> Route:
    for worker in (WORKER_A, WORKER_B):
        await store.upsert_worker(
            worker_id=worker["worker_id"],
            epoch=worker["worker_epoch"],
            endpoint=f"http://{worker['worker_id']}:8080",
            status="ACTIVE",
            running=0,
        )
    route = await store.create_route(
        sandbox_id=sandbox_id, workspace_scope_id="tenant-a", worker=WORKER_A
    )
    await store.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
    current = await store.find_route(sandbox_id)
    assert current is not None
    return current


async def _exec(store: SqlAlchemyDatabase, route: Route, exec_id: str) -> Any:
    return await store.begin_exec(
        sandbox_id=route.sandbox_id,
        exec_id=exec_id,
        generation=route.generation,
        worker_id="worker-a",
        command=json.dumps({"argv": ["/bin/true"], "exec_scope": "thread-a"}),
        exec_scope="thread-a",
    )


async def _finish(store: SqlAlchemyDatabase, route: Route, exec_id: str) -> None:
    await store.finish_exec(
        sandbox_id=route.sandbox_id,
        exec_id=exec_id,
        status="SUCCEEDED",
        exit_code=0,
        stdout="",
        stderr="",
        truncated=False,
    )


def test_the_sql_adapter_implements_the_dormant_route_store(store: SqlAlchemyDatabase) -> None:
    # An adapter that does not would answer 501 SANDBOX_SUSPEND_UNSUPPORTED.
    assert as_dormant_route_store(store) is not None


async def test_suspend_is_a_claim_then_a_finish_and_repeats_as_a_no_op(
    store: SqlAlchemyDatabase,
) -> None:
    route = await _ready(store)

    assert await store.begin_suspend(route.sandbox_id, route.generation) is True
    assert await store.begin_suspend(route.sandbox_id, route.generation) is True
    await store.finish_suspend(route.sandbox_id, route.generation)
    suspended = await store.find_route(route.sandbox_id)
    assert suspended is not None and suspended.status == "SUSPENDED"
    assert await store.begin_suspend(route.sandbox_id, route.generation) is False
    with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
        await store.begin_suspend(route.sandbox_id, route.generation + 1)


async def test_a_running_command_refuses_the_suspend_and_a_suspend_refuses_commands(
    store: SqlAlchemyDatabase,
) -> None:
    route = await _ready(store)

    assert await _exec(store, route, "exec-1") is None
    with pytest.raises(RuntimeError, match="SANDBOX_SUSPEND_BUSY"):
        await store.begin_suspend(route.sandbox_id, route.generation)
    await _finish(store, route, "exec-1")
    assert await store.begin_suspend(route.sandbox_id, route.generation) is True
    with pytest.raises(RuntimeError, match="SANDBOX_SUSPENDED"):
        await _exec(store, route, "exec-2")
    await store.finish_suspend(route.sandbox_id, route.generation)
    with pytest.raises(RuntimeError, match="SANDBOX_SUSPENDED"):
        await _exec(store, route, "exec-3")


async def test_exec_admission_and_suspend_have_exactly_one_winner_every_time(
    store: SqlAlchemyDatabase,
) -> None:
    """The race the feature turns on, under the server's own row locks."""
    outcomes: set[str] = set()
    for round_ in range(12):
        route = await _ready(store, f"sandbox-race-{round_}")
        results = await asyncio.gather(
            _exec(store, route, f"exec-race-{round_}"),
            store.begin_suspend(route.sandbox_id, route.generation),
            return_exceptions=True,
        )
        exec_won = results[0] is None
        suspend_won = results[1] is True
        assert exec_won != suspend_won, results
        current = await store.find_route(route.sandbox_id)
        assert current is not None
        assert current.status == ("RUNNING" if exec_won else "SUSPENDING")
        outcomes.add("exec" if exec_won else "suspend")
    assert outcomes  # both may win over time; neither may ever both win


async def test_one_resume_wins_and_a_move_bumps_and_audits_the_generation(
    store: SqlAlchemyDatabase,
) -> None:
    route = await _ready(store)
    await store.begin_suspend(route.sandbox_id, route.generation)
    await store.finish_suspend(route.sandbox_id, route.generation)
    suspended = await store.find_route(route.sandbox_id)
    assert suspended is not None

    first, second = await asyncio.gather(
        store.begin_resume(
            suspended,
            WORKER_A,
            bump_generation=False,
            profile_hash="profile-integration",
            created_by="a",
        ),
        store.begin_resume(
            suspended,
            WORKER_A,
            bump_generation=False,
            profile_hash="profile-integration",
            created_by="b",
        ),
    )
    winners = [item for item in (first, second) if item is not None]
    assert len(winners) == 1 and winners[0].status == "ASSIGNED"
    assert winners[0].generation == route.generation

    # Back to sleep, then a resume on another worker: the generation moves.
    await store.abort_resume(route.sandbox_id, route.generation)
    again = await store.find_route(route.sandbox_id)
    assert again is not None and again.status == "SUSPENDED"
    moved = await store.begin_resume(
        again,
        WORKER_B,
        bump_generation=True,
        profile_hash="profile-integration",
        created_by="workspace:tenant-a",
    )
    assert moved is not None
    assert (moved.worker_id, moved.generation, moved.status) == (
        "worker-b",
        route.generation + 1,
        "ASSIGNED",
    )
    assert moved.last_release_reason == "RESUME_REASSIGNED"
    with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
        await _exec(store, route, "exec-old-generation")


async def test_retention_expiry_and_stalled_suspends_are_listed(
    store: SqlAlchemyDatabase,
) -> None:
    expired = await _ready(store, "sandbox-expired")
    stalled = await _ready(store, "sandbox-stalled")
    await store.begin_suspend(expired.sandbox_id, expired.generation)
    await store.finish_suspend(expired.sandbox_id, expired.generation)
    await store.begin_suspend(stalled.sandbox_id, stalled.generation)
    engine = store.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.execute(
            update(route_table)
            .where(route_table.c.sandbox_id == expired.sandbox_id)
            .values(last_active_at=utc_now_naive() - timedelta(hours=2))
        )
        await connection.execute(
            update(route_table)
            .where(route_table.c.sandbox_id == stalled.sandbox_id)
            .values(updated_at=utc_now_naive() - timedelta(minutes=10))
        )

    listed = await store.list_dormant_routes_to_reclaim(
        retention_seconds=3600, suspending_grace_seconds=300, limit=10
    )
    assert sorted((item.sandbox_id, item.status) for item in listed) == [
        ("sandbox-expired", "SUSPENDED"),
        ("sandbox-stalled", "SUSPENDING"),
    ]
    assert [
        item.sandbox_id
        for item in await store.list_dormant_routes_to_reclaim(
            retention_seconds=3 * 3600, suspending_grace_seconds=300, limit=10
        )
    ] == ["sandbox-stalled"]
    # Zero disables retention but not the completion of a stalled suspend.
    assert [
        item.sandbox_id
        for item in await store.list_dormant_routes_to_reclaim(
            retention_seconds=0, suspending_grace_seconds=300, limit=10
        )
    ] == ["sandbox-stalled"]
    # An expired suspended route goes through the ordinary release.
    assert await store.begin_release(expired.sandbox_id, expired.generation) is True
    await store.release_route(
        expired.sandbox_id, expired.generation, reason="SUSPEND_EXPIRED", released_by="system"
    )
    released = await store.find_route(expired.sandbox_id)
    assert released is not None and released.status == "RELEASED"
    assert released.last_release_reason == "SUSPEND_EXPIRED"


# ── the snapshot on a real S3 API ──


@pytest.fixture
def blob_store(minio_endpoint: str) -> Iterator[BlobStore]:
    import boto3
    from botocore.config import Config

    from agent_sandbox.config import Settings

    client = boto3.client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id="integration",
        aws_secret_access_key="integration-secret",
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=BUCKET)
    except (client.exceptions.BucketAlreadyOwnedByYou, client.exceptions.BucketAlreadyExists):
        pass
    prefix = f"dormant-{time.time_ns()}/"
    settings = Settings(
        internal_token="integration-token",
        blobstore_endpoint=minio_endpoint,
        blobstore_bucket=BUCKET,
        blobstore_base_prefix=prefix,
        blobstore_region="us-east-1",
    )
    instance = BlobStore(settings, client=client)
    try:
        yield instance
    finally:
        listing = client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        contents = listing.get("Contents", [])
        if contents:
            client.delete_objects(
                Bucket=BUCKET, Delete={"Objects": [{"Key": item["Key"]} for item in contents]}
            )


def _keys(blob_store: BlobStore) -> list[str]:
    client = blob_store._s3()
    listing = client.list_objects_v2(Bucket=BUCKET, Prefix=blob_store.base_prefix)
    return sorted(item["Key"] for item in listing.get("Contents", []))


def test_a_snapshot_round_trips_through_s3_and_is_deleted_completely(
    blob_store: BlobStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    root = tmp_path / "sandbox"
    for name in ("workspace", "home", "envs", "cache", "logs"):
        (root / name).mkdir(parents=True)
    (root / "workspace" / "notes.txt").write_text("state before the wait")
    (root / "workspace" / "link").symlink_to("/etc/hostname")
    (root / "envs" / "marker").write_text("installed")
    limits = ArchiveLimits()

    write_snapshot(
        blob_store,
        sandbox_id="sb-1",
        generation=1,
        root=root,
        staging_root=tmp_path / "staging",
        limits=limits,
    )

    manifest = read_manifest(blob_store, "sb-1")
    assert manifest is not None and set(manifest["parts"]) == {"workspace", "home", "envs"}
    assert any(key.endswith(manifest_key("sb-1")) for key in _keys(blob_store))

    restored = tmp_path / "restored"
    result = restore_snapshot(
        blob_store,
        sandbox_id="sb-1",
        destination=restored,
        uid=os.getuid(),
        max_extract_bytes=64 * 1024 * 1024,
    )
    assert result is not None
    assert (restored / "workspace" / "notes.txt").read_text() == "state before the wait"
    assert os.readlink(restored / "workspace" / "link") == "/etc/hostname"
    assert (restored / "envs" / "marker").read_text() == "installed"

    # A newer snapshot replaces the older one's parts instead of piling up.
    (root / "workspace" / "notes.txt").write_text("second")
    write_snapshot(
        blob_store,
        sandbox_id="sb-1",
        generation=1,
        root=root,
        staging_root=tmp_path / "staging",
        limits=limits,
    )
    assert len([key for key in _keys(blob_store) if key.endswith(".tar.gz")]) == 3

    assert delete_snapshot(blob_store, "sb-1") is True
    assert _keys(blob_store) == []
    assert delete_snapshot(blob_store, "sb-1") is False
