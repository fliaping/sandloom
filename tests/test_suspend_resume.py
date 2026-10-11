"""Suspend and resume: release a sandbox's slot, keep its workspace, wake it later.

Three layers, each tested where its guarantee lives: the metadata store's
compare-and-set transitions (which make suspend and exec admission mutually
exclusive), the worker runtime (slot, directory, snapshot, disk eviction), and
the control plane's choice of where a resume lands. The end-to-end tests at the
bottom drive the HTTP API through two in-process workers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import update

from agent_sandbox.config import Settings
from agent_sandbox.dormant import manifest_key, read_manifest
from agent_sandbox.models import Route, utc_now_naive
from agent_sandbox.runtime import LocalSandbox, SandboxRuntime
from agent_sandbox.schemas import ExecRequest, ExecResponse
from agent_sandbox.service import SandboxService
from agent_sandbox.sql_database import SqlAlchemyDatabase, route_table


@pytest.fixture(autouse=True)
def _allow_chown_without_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workers chown to the sandbox UID, which needs root on a real worker."""
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)


class DirectoryStore:
    """An object store on a local directory, with the template store's contract."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / key

    def upload_file(self, key: str, path: str | Path) -> str:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        return f"blob://{key}"

    def download_to(self, key_or_uri: str, destination: str | Path) -> None:
        source = self._path(key_or_uri.removeprefix("blob://"))
        if not source.exists():
            raise FileNotFoundError(key_or_uri)
        shutil.copyfile(source, destination)

    def delete(self, key_or_uri: str) -> None:
        self._path(key_or_uri.removeprefix("blob://")).unlink(missing_ok=True)

    def keys(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file()
        )


# ── metadata store transitions ──


async def _store(tmp_path: Path, **overrides: Any) -> tuple[SqlAlchemyDatabase, Route]:
    pytest.importorskip("aiosqlite")
    settings = Settings(
        internal_token="token",
        local_root=tmp_path / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        profile_hash="profile-a",
        **overrides,
    )
    database = SqlAlchemyDatabase(settings)
    await database.connect()
    route = await database.create_route(
        sandbox_id="sandbox-a",
        workspace_scope_id="tenant-a",
        worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
    )
    await database.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
    current = await database.find_route(route.sandbox_id)
    assert current is not None
    return database, current


async def _begin_exec(database: SqlAlchemyDatabase, route: Route, exec_id: str) -> Any:
    return await database.begin_exec(
        sandbox_id=route.sandbox_id,
        exec_id=exec_id,
        generation=route.generation,
        worker_id="worker-a",
        command=json.dumps({"argv": ["/bin/true"], "exec_scope": "thread-a"}),
        exec_scope="thread-a",
    )


async def test_suspend_is_a_claim_then_a_finish_and_repeats_as_a_no_op(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        assert await database.begin_suspend(route.sandbox_id, route.generation) is True
        # A retry of a suspend that stalled midway is told to finish it.
        assert await database.begin_suspend(route.sandbox_id, route.generation) is True
        await database.finish_suspend(route.sandbox_id, route.generation)
        suspended = await database.find_route(route.sandbox_id)
        assert suspended is not None
        assert suspended.status == "SUSPENDED"
        assert suspended.generation == route.generation
        # Already suspended: nothing to do, and nothing is done.
        assert await database.begin_suspend(route.sandbox_id, route.generation) is False
        with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
            await database.begin_suspend(route.sandbox_id, route.generation + 1)
    finally:
        await database.close()


async def test_a_running_command_refuses_the_suspend_and_a_suspend_refuses_commands(
    tmp_path: Path,
) -> None:
    database, route = await _store(tmp_path)
    try:
        assert await _begin_exec(database, route, "exec-1") is None
        with pytest.raises(RuntimeError, match="SANDBOX_SUSPEND_BUSY"):
            await database.begin_suspend(route.sandbox_id, route.generation)
        await database.finish_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-1",
            status="SUCCEEDED",
            exit_code=0,
            stdout="",
            stderr="",
            truncated=False,
        )
        assert await database.begin_suspend(route.sandbox_id, route.generation) is True
        # Claimed: admission is refused with a code that says why.
        with pytest.raises(RuntimeError, match="SANDBOX_SUSPENDED"):
            await _begin_exec(database, route, "exec-2")
        await database.finish_suspend(route.sandbox_id, route.generation)
        with pytest.raises(RuntimeError, match="SANDBOX_SUSPENDED"):
            await _begin_exec(database, route, "exec-3")
        # A replayed exec_id still answers from its record, suspended or not.
        replay = await _begin_exec(database, route, "exec-1")
        assert replay is not None and replay["status"] == "SUCCEEDED"
    finally:
        await database.close()


async def test_exec_admission_and_suspend_race_to_exactly_one_winner(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        outcomes = await asyncio.gather(
            _begin_exec(database, route, "exec-race"),
            database.begin_suspend(route.sandbox_id, route.generation),
            return_exceptions=True,
        )
        exec_won = outcomes[0] is None
        suspend_won = outcomes[1] is True
        assert exec_won != suspend_won, outcomes
        current = await database.find_route(route.sandbox_id)
        assert current is not None
        assert current.status == ("RUNNING" if exec_won else "SUSPENDING")
    finally:
        await database.close()


async def test_abort_suspend_returns_the_route_to_ready(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        assert await database.begin_suspend(route.sandbox_id, route.generation)
        await database.abort_suspend(route.sandbox_id, route.generation)
        current = await database.find_route(route.sandbox_id)
        assert current is not None and current.status == "READY"
        assert await _begin_exec(database, route, "exec-after-abort") is None
    finally:
        await database.close()


async def test_resume_in_place_keeps_the_generation_and_only_one_resume_wins(
    tmp_path: Path,
) -> None:
    database, route = await _store(tmp_path)
    worker = {"worker_id": "worker-a", "worker_epoch": "epoch-a"}
    try:
        await database.begin_suspend(route.sandbox_id, route.generation)
        await database.finish_suspend(route.sandbox_id, route.generation)
        suspended = await database.find_route(route.sandbox_id)
        assert suspended is not None
        first, second = await asyncio.gather(
            database.begin_resume(
                suspended, worker, bump_generation=False, profile_hash="profile-a", created_by="a"
            ),
            database.begin_resume(
                suspended, worker, bump_generation=False, profile_hash="profile-a", created_by="b"
            ),
        )
        winners = [item for item in (first, second) if item is not None]
        assert len(winners) == 1
        assert winners[0].status == "ASSIGNED"
        assert winners[0].generation == route.generation
        await database.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
        assert await _begin_exec(database, route, "exec-after-resume") is None
    finally:
        await database.close()


async def test_resume_elsewhere_bumps_the_generation_and_audits_it(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        await database.begin_suspend(route.sandbox_id, route.generation)
        await database.finish_suspend(route.sandbox_id, route.generation)
        suspended = await database.find_route(route.sandbox_id)
        assert suspended is not None
        moved = await database.begin_resume(
            suspended,
            {"worker_id": "worker-b", "worker_epoch": "epoch-b"},
            bump_generation=True,
            profile_hash="profile-a",
            created_by="workspace:tenant-a",
        )
        assert moved is not None
        assert (moved.worker_id, moved.generation, moved.status) == (
            "worker-b",
            route.generation + 1,
            "ASSIGNED",
        )
        assert moved.last_release_reason == "RESUME_REASSIGNED"
        # The old generation is fenced: nothing can run against it any more.
        with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
            await _begin_exec(database, route, "exec-old-generation")
        # A failed resume goes back to sleep where a retry finds it.
        await database.abort_resume(moved.sandbox_id, moved.generation)
        current = await database.find_route(route.sandbox_id)
        assert current is not None and current.status == "SUSPENDED"
    finally:
        await database.close()


async def test_the_retention_clock_is_not_reset_and_expiry_is_listed(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        await database.begin_suspend(route.sandbox_id, route.generation)
        await database.finish_suspend(route.sandbox_id, route.generation)
        async with database._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(route_table.c.sandbox_id == route.sandbox_id)
                .values(last_active_at=utc_now_naive() - timedelta(hours=2))
            )
        before = await database.find_route(route.sandbox_id)
        await database.touch_route(route.sandbox_id, route.generation)
        after = await database.find_route(route.sandbox_id)
        assert before is not None and after is not None
        assert after.last_active_at == before.last_active_at

        assert (
            await database.list_dormant_routes_to_reclaim(
                retention_seconds=3 * 3600, suspending_grace_seconds=300, limit=10
            )
            == []
        )
        expired = await database.list_dormant_routes_to_reclaim(
            retention_seconds=3600, suspending_grace_seconds=300, limit=10
        )
        assert [item.sandbox_id for item in expired] == [route.sandbox_id]
        # Zero disables expiry.
        assert (
            await database.list_dormant_routes_to_reclaim(
                retention_seconds=0, suspending_grace_seconds=300, limit=10
            )
            == []
        )
        # A suspended route can still be released by its client.
        assert await database.begin_release(route.sandbox_id, route.generation) is True
    finally:
        await database.close()


async def test_a_stalled_suspend_is_listed_for_completion(tmp_path: Path) -> None:
    database, route = await _store(tmp_path)
    try:
        await database.begin_suspend(route.sandbox_id, route.generation)
        async with database._engine().begin() as connection:
            await connection.execute(
                update(route_table)
                .where(route_table.c.sandbox_id == route.sandbox_id)
                .values(updated_at=utc_now_naive() - timedelta(minutes=10))
            )
        stalled = await database.list_dormant_routes_to_reclaim(
            retention_seconds=0, suspending_grace_seconds=300, limit=10
        )
        assert [(item.sandbox_id, item.status) for item in stalled] == [
            (route.sandbox_id, "SUSPENDING")
        ]
    finally:
        await database.close()


# ── worker runtime ──


def _runtime(tmp_path: Path, name: str = "worker-a", **overrides: Any) -> SandboxRuntime:
    settings = Settings(
        internal_token="token",
        local_root=tmp_path / name / "sandboxes",
        template_root=tmp_path / name / "templates",
        min_free_bytes=0,
        **overrides,
    )
    return SandboxRuntime(settings)


async def _live(runtime: SandboxRuntime, sandbox_id: str = "sb-1") -> LocalSandbox:
    sandbox = await runtime.create(sandbox_id, 1, os.getuid())
    (sandbox.workspace / "notes.txt").write_text("state before the wait")
    (sandbox.root / "envs" / "venv").mkdir()
    (sandbox.root / "envs" / "venv" / "marker").write_text("installed")
    return sandbox


async def test_suspend_releases_the_slot_and_keeps_the_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    sandbox = await _live(runtime)

    result = await runtime.suspend("sb-1", 1)

    assert result == {"status": "SUSPENDED", "snapshot": False}
    assert "sb-1" not in runtime.sandboxes  # the slot the heartbeat counts
    assert runtime.dormant["sb-1"].generation == 1
    assert (sandbox.workspace / "notes.txt").read_text() == "state before the wait"
    # Repeating is a no-op.
    assert await runtime.suspend("sb-1", 1) == {"status": "SUSPENDED", "snapshot": False}
    with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
        runtime.get("sb-1", 1)


async def test_a_stale_handle_cannot_touch_a_suspended_workspace(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    sandbox = await _live(runtime)
    await runtime.suspend("sb-1", 1)

    with pytest.raises(RuntimeError, match="SANDBOX_SUSPENDED"):
        await runtime.write_file(sandbox, "/workspace/late.txt", base64.b64encode(b"x").decode())
    assert not (sandbox.workspace / "late.txt").exists()


async def test_suspend_refuses_while_a_command_holds_the_sandbox(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    sandbox = await _live(runtime)
    other = await _live(runtime, "sb-2")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute_locked(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        entered.set()
        await release.wait()
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = execute_locked  # type: ignore[method-assign]
    running = asyncio.create_task(
        runtime.execute(
            sandbox,
            ExecRequest(exec_id="exec-1", generation=1, argv=["true"], exec_scope="thread-a"),
        )
    )
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="SANDBOX_SUSPEND_BUSY"):
            await runtime.suspend("sb-1", 1)
        assert "sb-1" in runtime.sandboxes
        # Another sandbox is not affected by this one being busy.
        await runtime.suspend("sb-2", 1)
        assert "sb-2" in runtime.dormant
    finally:
        release.set()
        await running
    assert other.root.exists()
    await runtime.suspend("sb-1", 1)
    assert "sb-1" in runtime.dormant


async def test_resume_on_the_same_worker_reuses_the_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    sandbox = await _live(runtime)
    await runtime.suspend("sb-1", 1)

    resumed, source = await runtime.resume("sb-1", 1, os.getuid())

    assert source == "reused"
    assert resumed.root == sandbox.root
    assert "sb-1" in runtime.sandboxes and "sb-1" not in runtime.dormant
    assert (resumed.workspace / "notes.txt").read_text() == "state before the wait"
    # Idempotent: a second resume finds it awake.
    again, source = await runtime.resume("sb-1", 1, os.getuid())
    assert (again, source) == (resumed, "active")


async def test_resume_without_a_directory_or_snapshot_reports_the_loss(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    sandbox = await _live(runtime)
    await runtime.suspend("sb-1", 1)
    shutil.rmtree(sandbox.root)

    with pytest.raises(RuntimeError, match="SANDBOX_WORKSPACE_LOST"):
        await runtime.resume("sb-1", 2, os.getuid())
    assert "sb-1" not in runtime.sandboxes


async def test_a_snapshot_restores_the_workspace_on_another_worker(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    source_worker = _runtime(tmp_path, "worker-a")
    source_worker.templates.object_store = store
    sandbox = await _live(source_worker)
    (sandbox.workspace / "link").symlink_to("/etc/hostname")

    assert await source_worker.suspend("sb-1", 1, snapshot=True) == {
        "status": "SUSPENDED",
        "snapshot": True,
    }
    manifest = read_manifest(store, "sb-1")
    assert manifest is not None and set(manifest["parts"]) == {"workspace", "home", "envs"}

    target_worker = _runtime(tmp_path, "worker-b")
    target_worker.templates.object_store = store
    resumed, source = await target_worker.resume("sb-1", 2, os.getuid(), restore="snapshot")

    assert source == "restored"
    assert (resumed.workspace / "notes.txt").read_text() == "state before the wait"
    assert (resumed.root / "envs" / "venv" / "marker").read_text() == "installed"
    # Links come back as links, never as the file they point at.
    assert os.readlink(resumed.workspace / "link") == "/etc/hostname"
    for name in ("cache", "logs"):
        assert (resumed.root / name).is_dir()


async def test_a_new_snapshot_replaces_the_previous_one(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    runtime = _runtime(tmp_path)
    runtime.templates.object_store = store
    sandbox = await _live(runtime)
    await runtime.suspend("sb-1", 1, snapshot=True)
    first = read_manifest(store, "sb-1")
    await runtime.resume("sb-1", 1, os.getuid())
    (sandbox.workspace / "notes.txt").write_text("second")
    await runtime.suspend("sb-1", 1, snapshot=True)
    second = read_manifest(store, "sb-1")

    assert first is not None and second is not None
    assert first["nonce"] != second["nonce"]
    assert not any(first["nonce"] in key for key in store.keys())


async def test_snapshot_always_without_a_store_refuses_and_keeps_the_sandbox(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.templates.object_store = None
    await _live(runtime)

    with pytest.raises(RuntimeError, match="SANDBOX_SNAPSHOT_UNAVAILABLE"):
        await runtime.suspend("sb-1", 1, snapshot=True)
    assert "sb-1" in runtime.sandboxes and "sb-1" not in runtime.dormant


async def test_disk_pressure_evicts_only_snapshotted_dormant_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DirectoryStore(tmp_path / "store")
    runtime = _runtime(tmp_path)
    runtime.templates.object_store = store
    backed_up = await _live(runtime, "sb-backed-up")
    only_copy = await _live(runtime, "sb-only-copy")
    await runtime.suspend("sb-backed-up", 1, snapshot=True)
    await runtime.suspend("sb-only-copy", 1, snapshot=False)
    pressure = {"available": False}
    real_status = runtime.disk_status

    def disk_status() -> dict[str, int | bool]:
        return {**real_status(), "available": pressure["available"]}

    monkeypatch.setattr(runtime, "disk_status", disk_status)

    assert await runtime.reclaim_dormant_disk() == ["sb-backed-up"]
    assert not backed_up.root.exists()
    assert only_copy.root.exists()
    assert runtime.dormant["sb-backed-up"].evicted

    pressure["available"] = True
    resumed, source = await runtime.resume("sb-backed-up", 1, os.getuid())
    assert source == "restored"
    assert (resumed.workspace / "notes.txt").read_text() == "state before the wait"


# ── where a resume lands ──


def _route(**overrides: Any) -> Route:
    base = Route(
        sandbox_id="sb-1",
        workspace_scope_id="tenant-a",
        worker_id="worker-a",
        worker_epoch="epoch-a",
        generation=4,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-a",
        status="SUSPENDED",
        storage_mode="local",
    )
    return replace(base, **overrides)


def _worker(worker_id: str, epoch: str, **overrides: Any) -> dict[str, Any]:
    return {
        "worker_id": worker_id,
        "worker_epoch": epoch,
        "endpoint": f"http://{worker_id}:8080",
        "status": "ACTIVE",
        "profile_hash": "profile-a",
        "capacity": 4,
        "running_sessions": 1,
        **overrides,
    }


class _Registry:
    def __init__(self, *workers: dict[str, Any]) -> None:
        self.workers = {item["worker_id"]: item for item in workers}

    async def get(self, worker_id: str | None) -> dict[str, Any] | None:
        return self.workers.get(worker_id or "")

    async def select(self, *, profile_hash: str, exclude: set[str] | None = None) -> dict[str, Any]:
        for item in self.workers.values():
            if item["worker_id"] not in (exclude or set()):
                return item
        raise RuntimeError("NO_SANDBOX_WORKER_AVAILABLE")


def _planner(tmp_path: Path, registry: _Registry, *, store: Any = None) -> SandboxService:
    service = SandboxService(
        Settings(internal_token="token", profile_hash="profile-a", local_root=tmp_path),
        object(),  # type: ignore[arg-type]
        registry,  # type: ignore[arg-type]
    )
    service.template_catalog.object_store = store
    return service


async def test_a_live_original_worker_reuses_the_directory_in_place(tmp_path: Path) -> None:
    service = _planner(tmp_path, _Registry(_worker("worker-a", "epoch-a")))
    worker, bump, restore = await service._resume_plan(_route())
    assert (worker["worker_id"], bump, restore) == ("worker-a", False, "reuse")


async def test_a_restarted_original_worker_reuses_with_a_new_generation(tmp_path: Path) -> None:
    service = _planner(tmp_path, _Registry(_worker("worker-a", "epoch-restarted")))
    worker, bump, restore = await service._resume_plan(_route())
    assert (worker["worker_id"], bump, restore) == ("worker-a", True, "reuse")


async def test_a_shared_workspace_moves_to_any_worker(tmp_path: Path) -> None:
    service = _planner(tmp_path, _Registry(_worker("worker-b", "epoch-b")))
    worker, bump, restore = await service._resume_plan(_route(storage_mode="shared"))
    assert (worker["worker_id"], bump, restore) == ("worker-b", True, "reuse")


async def test_a_local_workspace_moves_only_with_a_snapshot(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "store")
    registry = _Registry(_worker("worker-b", "epoch-b"))
    service = _planner(tmp_path, registry, store=store)

    with pytest.raises(RuntimeError, match="SANDBOX_DORMANT_WORKSPACE_UNAVAILABLE"):
        await service._resume_plan(_route())

    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"sandbox_id": "sb-1", "nonce": "n", "parts": {}}))
    store.upload_file(manifest_key("sb-1"), manifest)
    worker, bump, restore = await service._resume_plan(_route())
    assert (worker["worker_id"], bump, restore) == ("worker-b", True, "snapshot")


async def test_a_full_original_worker_without_a_snapshot_asks_for_a_retry(
    tmp_path: Path,
) -> None:
    full = _worker("worker-a", "epoch-a", running_sessions=4)
    service = _planner(tmp_path, _Registry(full))
    with pytest.raises(RuntimeError, match="NO_SANDBOX_WORKER_AVAILABLE"):
        await service._resume_plan(_route())


# ── end to end, over HTTP, across two workers ──

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
_RealAsyncClient = httpx.AsyncClient


class _Fleet(httpx.AsyncBaseTransport):
    """Routes worker-to-worker calls to in-process apps by host name."""

    def __init__(self) -> None:
        self.apps: dict[str, httpx.ASGITransport] = {}

    def add(self, host: str, app: Any) -> None:
        self.apps[host] = httpx.ASGITransport(app=app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self.apps.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError("worker is down", request=request)
        return await transport.handle_async_request(request)


async def _worker_app(
    tmp_path: Path, name: str, fleet: _Fleet, store: DirectoryStore, registry: Any = None
) -> Any:
    from agent_sandbox.app import create_app

    settings = Settings(
        internal_token=TOKEN,
        advertise_host=name,
        port=8080,
        local_root=tmp_path / name / "sandboxes",
        template_root=tmp_path / name / "templates",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        min_free_bytes=0,
        profile_hash="profile-a",
        # One UID the test process owns, so a reused directory validates.
        uid_start=os.getuid(),
        uid_end=os.getuid(),
        suspend_snapshot="always",
    )
    app = create_app(settings)
    service = app.state.sandbox_service
    if registry is not None:
        service.registry = registry
    service.template_catalog.object_store = store
    service.runtime.templates.object_store = store
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    await service.database.connect()
    await service._heartbeat()
    fleet.add(name, app)
    return app


@pytest.fixture
async def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    pytest.importorskip("aiosqlite")
    transport = _Fleet()

    def client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return _RealAsyncClient(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    store = DirectoryStore(tmp_path / "store")
    app_a = await _worker_app(tmp_path, "worker-a", transport, store)
    apps = [app_a]
    yield transport, store, apps
    for app in apps:
        await app.state.sandbox_service.database.close()


async def _api(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async with _RealAsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=AUTH, **kwargs)


async def _ready_sandbox(app: Any) -> int:
    resolved = await _api(
        app,
        "POST",
        "/api/v1/sandboxes/resolve",
        json={"sandbox_id": "agent-1", "workspace_scope_id": "tenant-a"},
    )
    assert resolved.status_code == 200, resolved.text
    generation = int(resolved.json()["generation"])
    created = await _api(app, "POST", "/api/v1/sandboxes/agent-1", json={"generation": generation})
    assert created.status_code == 200, created.text
    written = await _api(
        app,
        "PUT",
        "/api/v1/sandboxes/agent-1/files",
        json={
            "generation": generation,
            "path": "/workspace/progress.txt",
            "content_base64": base64.b64encode(b"step 41 of 90").decode(),
        },
    )
    assert written.status_code == 200, written.text
    # What the heartbeat loop would publish within its interval.
    await app.state.sandbox_service._heartbeat()
    return generation


async def _read(app: Any, generation: int) -> httpx.Response:
    return await _api(
        app,
        "GET",
        "/api/v1/sandboxes/agent-1/files",
        params={"path": "/workspace/progress.txt", "generation": generation},
    )


async def _running_sessions(app: Any) -> int:
    service = app.state.sandbox_service
    worker = await service.registry.get(service.worker_id)
    return int(worker["running_sessions"])


async def test_suspend_then_resume_in_place_over_http(fleet: Any) -> None:
    _, store, (app,) = fleet
    generation = await _ready_sandbox(app)
    assert await _running_sessions(app) == 1

    suspended = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )
    assert suspended.status_code == 200, suspended.text
    body = suspended.json()
    assert (body["status"], body["snapshot"], body["suspended"]) == ("SUSPENDED", True, True)
    assert body["retained_until"] is not None
    # The slot is back, and published at once.
    assert await _running_sessions(app) == 0

    exec_response = await _api(
        app,
        "POST",
        "/api/v1/sandboxes/agent-1/exec",
        json={"exec_id": "exec-1", "generation": generation, "argv": ["true"]},
    )
    assert (exec_response.status_code, exec_response.text) == (409, "SANDBOX_SUSPENDED")
    assert (await _read(app, generation)).status_code == 409

    again = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )
    assert again.status_code == 200 and again.json()["suspended"] is False

    resumed = await _api(app, "POST", "/api/v1/sandboxes/agent-1/resume")
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert (body["status"], body["generation"], body["resumed"], body["workspace_source"]) == (
        "READY",
        generation,
        True,
        "reused",
    )
    assert await _running_sessions(app) == 1
    # A snapshot of a sandbox that is running again is stale; it is removed.
    assert store.keys() == []
    content = await _read(app, generation)
    assert content.status_code == 200, content.text
    assert base64.b64decode(content.json()["content_base64"]) == b"step 41 of 90"

    repeat = await _api(app, "POST", "/api/v1/sandboxes/agent-1/resume")
    assert repeat.status_code == 200
    assert (repeat.json()["resumed"], repeat.json()["workspace_source"]) == (False, "active")


async def test_resolve_wakes_a_suspended_sandbox(fleet: Any) -> None:
    _, _, (app,) = fleet
    generation = await _ready_sandbox(app)
    await _api(app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})

    resolved = await _api(
        app,
        "POST",
        "/api/v1/sandboxes/resolve",
        json={"sandbox_id": "agent-1", "workspace_scope_id": "tenant-a"},
    )

    assert resolved.status_code == 200, resolved.text
    assert (resolved.json()["status"], resolved.json()["generation"]) == ("READY", generation)
    assert (await _read(app, generation)).status_code == 200


async def test_a_busy_sandbox_refuses_to_suspend_over_http(fleet: Any) -> None:
    _, _, (app,) = fleet
    generation = await _ready_sandbox(app)
    database = app.state.sandbox_service.database
    await database.begin_exec(
        sandbox_id="agent-1",
        exec_id="exec-long",
        generation=generation,
        worker_id="worker-a-8080",
        command=json.dumps({"argv": ["sleep", "600"], "exec_scope": "thread-a"}),
        exec_scope="thread-a",
    )

    response = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )

    assert (response.status_code, response.text) == (409, "SANDBOX_SUSPEND_BUSY")
    route = await database.find_route("agent-1")
    assert route is not None and route.status == "RUNNING"
    assert await _running_sessions(app) == 1


async def test_resume_moves_to_another_worker_from_the_snapshot(
    fleet: Any, tmp_path: Path
) -> None:
    transport, store, apps = fleet
    app_a = apps[0]
    generation = await _ready_sandbox(app_a)
    suspended = await _api(
        app_a, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )
    assert suspended.json()["snapshot"] is True

    registry = app_a.state.sandbox_service.registry
    app_b = await _worker_app(tmp_path, "worker-b", transport, store, registry=registry)
    apps.append(app_b)
    # Worker A goes away with the only local copy.
    await registry.unregister(app_a.state.sandbox_service.worker_id)
    del transport.apps["worker-a"]

    resumed = await _api(app_b, "POST", "/api/v1/sandboxes/agent-1/resume")

    assert resumed.status_code == 200, resumed.text
    body = resumed.json()
    assert (body["worker_id"], body["generation"], body["workspace_source"]) == (
        "worker-b-8080",
        generation + 1,
        "restored",
    )
    content = await _read(app_b, generation + 1)
    assert content.status_code == 200, content.text
    assert base64.b64decode(content.json()["content_base64"]) == b"step 41 of 90"
    # The old generation is fenced.
    assert (await _read(app_b, generation)).status_code == 409


async def test_resume_without_a_copy_anywhere_is_retryable_not_destructive(
    fleet: Any, tmp_path: Path
) -> None:
    transport, store, apps = fleet
    app_a = apps[0]
    service_a = app_a.state.sandbox_service
    service_a.settings.suspend_snapshot = "never"
    generation = await _ready_sandbox(app_a)
    await _api(app_a, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})
    app_b = await _worker_app(tmp_path, "worker-b", transport, store, registry=service_a.registry)
    apps.append(app_b)
    await service_a.registry.unregister(service_a.worker_id)

    response = await _api(app_b, "POST", "/api/v1/sandboxes/agent-1/resume")

    assert (response.status_code, response.text) == (503, "SANDBOX_DORMANT_WORKSPACE_UNAVAILABLE")
    route = await service_a.database.find_route("agent-1")
    assert route is not None
    assert (route.status, route.worker_id, route.generation) == (
        "SUSPENDED",
        service_a.worker_id,
        generation,
    )


async def test_retention_expiry_releases_and_reclaims_the_disk(fleet: Any) -> None:
    _, store, (app,) = fleet
    service = app.state.sandbox_service
    generation = await _ready_sandbox(app)
    await _api(app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})
    root = service.settings.workspace_root / "agent-1"
    assert root.exists() and store.keys()
    async with service.database._engine().begin() as connection:
        await connection.execute(
            update(route_table)
            .where(route_table.c.sandbox_id == "agent-1")
            .values(
                last_active_at=utc_now_naive()
                - timedelta(seconds=service.settings.suspended_retention_seconds + 60)
            )
        )

    await service._maintenance()

    route = await service.database.find_route("agent-1")
    assert route is not None
    assert (route.status, route.last_release_reason, route.last_released_by) == (
        "RELEASED",
        "SUSPEND_EXPIRED",
        "system:orphan-reaper",
    )
    assert not root.exists()
    assert "agent-1" not in service.runtime.dormant
    assert store.keys() == []
    # Counted by the same reaper counters as an idle-timeout release.
    assert service.reaper_status["last_released"] == 1
    assert service.reaper_status["released_total"] == 1


# ── workspace directories left behind on a worker that no longer owns them ──


def _age(marker: Path, seconds: float) -> None:
    then = time.time() - seconds
    os.utime(marker, (then, then))


async def test_an_orphaned_directory_is_deleted_only_after_its_ttl(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    active = await _live(runtime, "sb-active")
    stale = await _live(runtime, "sb-stale")
    # What a resume elsewhere leaves: a directory no sandbox here holds.
    runtime.sandboxes.pop("sb-stale")

    # A live sandbox is never even listed.
    assert await runtime.local_workspace_ids() == ["sb-stale"]

    # The first sighting starts the clock and deletes nothing.
    assert await runtime.reclaim_orphan_workspaces(["sb-stale"], ttl_seconds=3600) == []
    marker = runtime._orphan_marker_root() / "sb-stale"
    assert marker.exists() and stale.root.exists()
    _age(marker, 3000)
    assert await runtime.reclaim_orphan_workspaces(["sb-stale"], ttl_seconds=3600) == []
    assert stale.root.exists()

    # A restarted worker reads the clock from disk instead of starting over.
    restarted = _runtime(tmp_path)
    _age(marker, 3601)
    assert await restarted.reclaim_orphan_workspaces(["sb-stale"], ttl_seconds=3600) == [
        "sb-stale"
    ]
    assert not stale.root.exists()
    assert not marker.exists()
    assert (active.workspace / "notes.txt").exists()


async def test_a_live_sandbox_is_never_deleted_even_if_reported_orphaned(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    active = await _live(runtime, "sb-active")
    markers = runtime._orphan_marker_root()
    markers.mkdir(parents=True, exist_ok=True)
    (markers / "sb-active").touch()
    _age(markers / "sb-active", 10_000)

    assert await runtime.reclaim_orphan_workspaces(["sb-active"], ttl_seconds=60) == []
    assert active.root.exists()
    assert list(markers.iterdir()) == []


async def test_a_dormant_entry_whose_route_was_released_elsewhere_is_reclaimed(
    tmp_path: Path,
) -> None:
    """The route decides, not memory: a release that could not reach this
    worker leaves a dormant entry here that nothing else would ever clear."""
    runtime = _runtime(tmp_path)
    dormant = await _live(runtime, "sb-dormant")
    await runtime.suspend("sb-dormant", 1)

    assert await runtime.local_workspace_ids() == ["sb-dormant"]
    await runtime.reclaim_orphan_workspaces(["sb-dormant"], ttl_seconds=60)
    _age(runtime._orphan_marker_root() / "sb-dormant", 61)

    assert await runtime.reclaim_orphan_workspaces(["sb-dormant"], ttl_seconds=60) == [
        "sb-dormant"
    ]
    assert not dormant.root.exists()
    assert "sb-dormant" not in runtime.dormant


async def test_an_orphan_that_is_owned_again_loses_its_clock(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    stale = await _live(runtime, "sb-stale")
    runtime.sandboxes.pop("sb-stale")
    await runtime.reclaim_orphan_workspaces(["sb-stale"], ttl_seconds=60)
    marker = runtime._orphan_marker_root() / "sb-stale"
    _age(marker, 120)

    # The control plane stopped reporting it: its route points here again.
    assert await runtime.reclaim_orphan_workspaces([], ttl_seconds=60) == []
    assert not marker.exists()
    # Reported again later, the clock starts from zero.
    assert await runtime.reclaim_orphan_workspaces(["sb-stale"], ttl_seconds=60) == []
    assert stale.root.exists()


async def test_the_old_worker_reclaims_the_copy_a_cross_worker_resume_left(
    fleet: Any, tmp_path: Path
) -> None:
    transport, store, apps = fleet
    app_a = apps[0]
    service_a = app_a.state.sandbox_service
    service_a.settings.orphan_dormant_dir_ttl_seconds = 60
    generation = await _ready_sandbox(app_a)
    await _api(app_a, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})
    old_root = service_a.settings.workspace_root / "agent-1"
    markers = service_a.runtime._orphan_marker_root()

    # Suspended here: listed (memory does not protect a dormant entry), but
    # its route names worker A, so no clock starts.
    await service_a._maintenance()
    assert old_root.exists()
    assert not (markers / "agent-1").exists()

    # Suspended here, and worker A restarted (its memory of the dormant
    # sandbox is gone): the route still names worker A, so it is kept.
    service_a.runtime.dormant.clear()
    await service_a._maintenance()
    assert old_root.exists()
    assert not (markers / "agent-1").exists()

    registry = service_a.registry
    app_b = await _worker_app(tmp_path, "worker-b", transport, store, registry=registry)
    apps.append(app_b)
    service_b = app_b.state.sandbox_service
    service_b.settings.orphan_dormant_dir_ttl_seconds = 60
    await registry.unregister(service_a.worker_id)
    resumed = await _api(app_b, "POST", "/api/v1/sandboxes/agent-1/resume")
    assert resumed.json()["workspace_source"] == "restored"

    # Worker A sees the copy is no longer its own and starts the clock.
    await service_a._maintenance()
    assert old_root.exists()
    assert (markers / "agent-1").exists()
    _age(markers / "agent-1", 61)
    await service_a._maintenance()

    assert not old_root.exists()
    assert service_a.reaper_status["orphan_workspaces_deleted_total"] == 1
    # The active copy on worker B is its own and stays.
    await service_b._maintenance()
    assert not (service_b.runtime._orphan_marker_root() / "agent-1").exists()
    content = await _read(app_b, generation + 1)
    assert base64.b64decode(content.json()["content_base64"]) == b"step 41 of 90"


async def test_a_ttl_of_zero_keeps_orphaned_directories(fleet: Any) -> None:
    _, _, (app,) = fleet
    service = app.state.sandbox_service
    service.settings.orphan_dormant_dir_ttl_seconds = 0
    service.runtime.local_workspace_ids = None  # would fail if it were consulted

    assert await service._reclaim_orphan_workspaces() == 0


async def test_an_expiry_that_fails_midway_is_finished_by_the_normal_retry(fleet: Any) -> None:
    """Same release path as an idle timeout: claim, worker delete, audit, retry.

    The first attempt cannot reach the worker and leaves the route RELEASING;
    the reaper's ordinary RELEASE_RETRY finishes it, and that retry has to
    delete the snapshot too, or it is left in the object store for good.
    """
    transport, store, (app,) = fleet
    service = app.state.sandbox_service
    generation = await _ready_sandbox(app)
    await _api(app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})
    root = service.settings.workspace_root / "agent-1"
    assert root.exists() and store.keys()
    async with service.database._engine().begin() as connection:
        await connection.execute(
            update(route_table)
            .where(route_table.c.sandbox_id == "agent-1")
            .values(
                last_active_at=utc_now_naive()
                - timedelta(seconds=service.settings.suspended_retention_seconds + 60)
            )
        )
    worker_app = transport.apps.pop("worker-a")

    await service._maintenance()

    route = await service.database.find_route("agent-1")
    assert route is not None and route.status == "RELEASING"
    assert service.reaper_status["failures_total"] == 1
    assert root.exists() and store.keys()

    transport.apps["worker-a"] = worker_app
    service.settings.orphan_release_grace_seconds = 0
    await service._maintenance()

    route = await service.database.find_route("agent-1")
    assert route is not None
    assert (route.status, route.last_release_reason, route.last_released_by) == (
        "RELEASED",
        "RELEASE_RETRY",
        "system:orphan-reaper",
    )
    assert not root.exists()
    assert store.keys() == []
    assert service.reaper_status["released_total"] == 1


# ── adapters written before suspend existed ──


class _WithoutDormantRoutes:
    """A metadata store that predates `DormantRouteStore`: everything else delegates."""

    _HIDDEN = frozenset(
        {
            "begin_suspend",
            "finish_suspend",
            "abort_suspend",
            "begin_resume",
            "abort_resume",
            "list_dormant_routes_to_reclaim",
        }
    )

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        if name in self._HIDDEN:
            raise AttributeError(name)
        return getattr(self._inner, name)


async def test_a_metadata_store_without_the_protocol_answers_501_and_keeps_working(
    fleet: Any,
) -> None:
    _, _, (app,) = fleet
    service = app.state.sandbox_service
    generation = await _ready_sandbox(app)
    service.database = _WithoutDormantRoutes(service.database)

    suspended = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )
    resumed = await _api(app, "POST", "/api/v1/sandboxes/agent-1/resume")

    assert (suspended.status_code, suspended.text) == (501, "SANDBOX_SUSPEND_UNSUPPORTED")
    assert (resumed.status_code, resumed.text) == (501, "SANDBOX_SUSPEND_UNSUPPORTED")
    assert (await _read(app, generation)).status_code == 200
    # The maintenance cycle skips the dormant sweep instead of failing.
    await service._maintenance()
    assert service.reaper_status["failures_total"] == 0


async def test_an_execution_backend_without_the_protocol_answers_501_and_stays_ready(
    fleet: Any,
) -> None:
    _, _, (app,) = fleet
    service = app.state.sandbox_service
    generation = await _ready_sandbox(app)
    service.runtime.suspend = None
    service.runtime.resume = None
    service.runtime.reclaim_dormant_disk = None

    suspended = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )

    assert (suspended.status_code, suspended.text) == (501, "SANDBOX_SUSPEND_UNSUPPORTED")
    route = await service.database.find_route("agent-1")
    assert route is not None and route.status == "READY"
    assert (await _read(app, generation)).status_code == 200
    await service._maintenance()
    assert service.reaper_status["failures_total"] == 0


# ── disk pressure after a restart ──


async def test_a_restarted_worker_still_evicts_its_snapshotted_dormant_copies(
    fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, (app,) = fleet
    service = app.state.sandbox_service
    generation = await _ready_sandbox(app)
    suspended = await _api(
        app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation}
    )
    assert suspended.json()["snapshot"] is True
    root = service.settings.workspace_root / "agent-1"
    # A restart: the directory stays, the worker's memory of it does not.
    service.runtime.dormant.clear()
    pressure = {"available": True}
    real_status = service.runtime.disk_status
    monkeypatch.setattr(
        service.runtime,
        "disk_status",
        lambda: {**real_status(), "available": pressure["available"]},
    )

    await service._maintenance()
    assert root.exists() and "agent-1" not in service.runtime.dormant  # no pressure: nothing to do

    pressure["available"] = False
    await service._maintenance()
    assert not root.exists()
    assert service.reaper_status["dormant_evicted_total"] == 1
    route = await service.database.find_route("agent-1")
    assert route is not None and route.status == "SUSPENDED"

    pressure["available"] = True
    resumed = await _api(app, "POST", "/api/v1/sandboxes/agent-1/resume")
    assert resumed.json()["workspace_source"] == "restored"
    assert (await _read(app, resumed.json()["generation"])).status_code == 200


async def test_a_restarted_worker_keeps_a_dormant_copy_that_has_no_snapshot(
    fleet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, (app,) = fleet
    service = app.state.sandbox_service
    service.settings.suspend_snapshot = "never"
    generation = await _ready_sandbox(app)
    await _api(app, "POST", "/api/v1/sandboxes/agent-1/suspend", json={"generation": generation})
    root = service.settings.workspace_root / "agent-1"
    service.runtime.dormant.clear()
    real_status = service.runtime.disk_status
    monkeypatch.setattr(
        service.runtime, "disk_status", lambda: {**real_status(), "available": False}
    )

    await service._maintenance()

    assert root.exists()
    assert "agent-1" not in service.runtime.dormant
