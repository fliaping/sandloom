"""Shared correctness boundaries, independent of the selected namespace Level."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import signal
import socket
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox import archive as archive_module
from agent_sandbox import runtime as runtime_module
from agent_sandbox.archive import ArchiveLimits
from agent_sandbox.config import Settings
from agent_sandbox.runtime import LocalSandbox, SandboxRuntime
from agent_sandbox.schemas import ExecRequest
from agent_sandbox.templates import (
    LocalTemplateCache,
    TemplateError,
    TemplateManager,
    TemplateRecord,
    archive_directory,
)
from agent_sandbox.threading import complete_in_thread
from agent_sandbox.workspace import open_directory


@pytest.fixture(autouse=True)
def no_chown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chown", lambda *_a, **_kw: None)


def tree(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"tenant data")
    return source


def test_pinned_source_survives_rename_without_following_replacement(tmp_path: Path) -> None:
    source = tree(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    (host / "secret").write_text("host secret")
    with open_directory(source) as fd:
        source.rename(tmp_path / "old")
        source.symlink_to(host, target_is_directory=True)
        archive_directory(fd, tmp_path / "result.tar.gz")
        assert os.fstat(fd)  # The archiver does not close the caller's descriptor.
    with tarfile.open(tmp_path / "result.tar.gz") as tar:
        assert tar.getnames() == ["data"]
        stream = tar.extractfile("data")
        assert stream is not None and stream.read() == b"tenant data"


@pytest.mark.parametrize("kind", ["file-link", "directory-link", "fifo", "file-replacement"])
def test_stat_open_races_never_read_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    source = tree(tmp_path)
    host = tmp_path / "secret"
    host.write_text("host secret")
    if kind == "directory-link":
        (source / "data").unlink()
        (source / "data").mkdir()
    original_open = os.open
    changed = False

    def replace_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal changed
        if path == "data" and kwargs.get("dir_fd") is not None and not changed:
            changed = True
            target = source / "data"
            if target.is_dir():
                target.rename(source / "old")
                target.symlink_to(tmp_path, target_is_directory=True)
            else:
                target.unlink()
                if kind == "fifo":
                    os.mkfifo(target)
                elif kind == "file-replacement":
                    target.write_text("host secret")
                else:
                    target.symlink_to(host)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replace_before_open)
    destination = tmp_path / "result.tar.gz"
    with pytest.raises(TemplateError):
        archive_directory(source, destination)
    assert changed
    assert not destination.exists()
    assert host.read_text() == "host secret"


def test_absolute_venv_link_is_preserved_without_reading_it(tmp_path: Path) -> None:
    source = tree(tmp_path)
    host = tmp_path / "secret"
    host.write_text("host secret")
    (source / "python").symlink_to(host)
    archive_directory(source, tmp_path / "result.tar.gz")
    with tarfile.open(tmp_path / "result.tar.gz") as tar:
        member = tar.getmember("python")
        assert member.issym() and member.linkname == str(host)
        assert member.size == 0


@pytest.mark.parametrize("kind", ["fifo", "socket"])
def test_special_files_are_refused(tmp_path: Path, kind: str) -> None:
    source = tree(tmp_path)
    with socket.socket(socket.AF_UNIX) as sock, tempfile.TemporaryDirectory(dir="/tmp") as short:
        if kind == "fifo":
            os.mkfifo(source / "special")
        else:
            shortcut = Path(short) / "src"
            shortcut.symlink_to(source, target_is_directory=True)
            sock.bind(str(shortcut / "special"))
        with pytest.raises(TemplateError, match="unsupported special file"):
            archive_directory(source, tmp_path / "result.tar.gz")
    assert not (tmp_path / "result.tar.gz").exists()


@pytest.mark.parametrize("mutation", ["grow", "shrink"])
def test_content_changes_abort_and_clean_partial_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    source = tree(tmp_path)
    original_read = archive_module._SnapshotReader.read
    changed = False

    def read_then_change(reader: Any, size: int) -> bytes:
        nonlocal changed
        result = original_read(reader, size)
        if not changed:
            changed = True
            (source / "data").write_bytes(b"longer changed contents" if mutation == "grow" else b"")
        return result

    monkeypatch.setattr(archive_module._SnapshotReader, "read", read_then_change)
    with pytest.raises(TemplateError, match="changed"):
        archive_directory(source, tmp_path / "result.tar.gz")
    assert not (tmp_path / "result.tar.gz").exists()


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_entries": 1}, "entry limit"),
        ({"max_source_bytes": 1}, "source byte limit"),
        ({"max_archive_bytes": 1}, "over the 1 byte limit"),
        ({"max_depth": 1}, "depth limit"),
    ],
)
def test_snapshot_budgets_clean_partial_output(
    tmp_path: Path, options: dict[str, Any], message: str
) -> None:
    source = tree(tmp_path)
    (source / "nested").mkdir()
    (source / "nested" / "file").write_text("data")
    destination = tmp_path / "result.tar.gz"
    with pytest.raises(TemplateError, match=message):
        archive_directory(source, destination, limits=ArchiveLimits(**options))
    assert not destination.exists()


def test_snapshot_deadline_is_checked_during_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tree(tmp_path)
    times = iter([0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(archive_module.time, "monotonic", lambda: next(times, 2.0))
    with pytest.raises(TemplateError, match="time limit"):
        archive_directory(
            source, tmp_path / "result.tar.gz", limits=ArchiveLimits(timeout_seconds=1)
        )
    assert not (tmp_path / "result.tar.gz").exists()


def test_invalid_snapshot_is_not_uploaded(tmp_path: Path) -> None:
    source = tree(tmp_path)
    (source / "escape").symlink_to("../../host")
    uploads: list[str] = []

    class Store:
        def upload_file(self, key: str, path: str | Path) -> str:
            uploads.append(key)
            return key

        def download_to(self, key: str, destination: str | Path) -> None:
            raise AssertionError("unexpected download")

        def delete(self, key: str) -> None:
            raise AssertionError("unexpected delete")

    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=Store())
    with pytest.raises(TemplateError, match="link escaping"):
        manager.build(name="invalid", source=source)
    assert uploads == []
    assert list(manager.cache.staging_root.iterdir()) == []
    assert manager.cache.list_revisions() == []


async def runtime_and_sandbox(tmp_path: Path) -> tuple[SandboxRuntime, LocalSandbox]:
    runtime = SandboxRuntime(
        Settings(
            internal_token="token",
            local_root=tmp_path,
            template_root=tmp_path / "templates",
            min_free_bytes=0,
            disk_high_watermark_percent=99,
        )
    )
    sandbox = await runtime.create("sandbox", 1, os.getuid())
    (sandbox.root / "envs" / "data").write_text("template data")
    return runtime, sandbox


@pytest.mark.parametrize("root", ["envs", "workspace"])
async def test_template_source_refuses_symlink_ancestors(tmp_path: Path, root: str) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    (host / "secret").write_text("host secret")
    (sandbox.root / root / "escape").symlink_to(host, target_is_directory=True)
    with pytest.raises(RuntimeError, match="SOURCE_NOT_A_DIRECTORY"):
        runtime.build_template(sandbox, name="safe", source_path=f"/{root}/escape")


async def test_runtime_snapshot_pins_source_before_ancestor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    host = tmp_path / "host"
    host.mkdir()
    (host / "secret").write_text("host secret")
    original_build = runtime.templates.build

    def replace_after_source_open(**kwargs: Any) -> TemplateRecord:
        (sandbox.root / "envs").rename(sandbox.root / "old-envs")
        (sandbox.root / "envs").symlink_to(host, target_is_directory=True)
        assert isinstance(kwargs["source"], int)
        return original_build(**kwargs)

    monkeypatch.setattr(runtime.templates, "build", replace_after_source_open)
    record = runtime.build_template(sandbox, name="safe")
    materialized = runtime.templates.cache.path_for(record.name, record.digest)
    assert (materialized / "data").read_text() == "template data"
    assert not (materialized / "secret").exists()


async def test_template_storage_failure_is_not_reported_as_invalid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)

    def unavailable(**kwargs: Any) -> TemplateRecord:
        raise OSError("object storage unavailable")

    monkeypatch.setattr(runtime.templates, "build", unavailable)
    with pytest.raises(OSError, match="object storage unavailable"):
        runtime.build_template(sandbox, name="safe")


@pytest.mark.parametrize("scope", [None, "task-a"])
async def test_snapshot_refuses_inflight_execution(tmp_path: Path, scope: str | None) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    with runtime._execution_lock(sandbox, scope), pytest.raises(RuntimeError, match="LOCKED"):
        runtime.build_template(sandbox, name="safe")


async def test_snapshot_refuses_inflight_file_api(tmp_path: Path) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    with runtime._path_lock(sandbox, "data"), pytest.raises(RuntimeError, match="LOCKED"):
        runtime.build_template(sandbox, name="safe")


async def test_snapshot_rechecks_generation_inside_lock(tmp_path: Path) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    runtime.sandboxes[sandbox.sandbox_id] = LocalSandbox("sandbox", 2, sandbox.uid, sandbox.root)
    with pytest.raises(RuntimeError, match="STALE_SANDBOX_GENERATION"):
        runtime.build_template(sandbox, name="safe")


async def test_cancelled_build_and_duplicate_release_wait_for_actual_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    entered = threading.Event()
    finish = threading.Event()
    original_build = runtime.templates.build

    def blocked_build(**kwargs: Any) -> TemplateRecord:
        entered.set()
        assert finish.wait(5), "test did not release the build worker"
        return original_build(**kwargs)

    monkeypatch.setattr(runtime.templates, "build", blocked_build)
    build = asyncio.create_task(complete_in_thread(runtime.build_template, sandbox, name="safe"))
    destroys: list[asyncio.Task[None]] = []
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        build.cancel()
        await asyncio.sleep(0)
        build.cancel()  # Repeated cancellation must not detach the thread.
        await asyncio.sleep(0)
        assert not build.done()
        with pytest.raises(RuntimeError, match="LOCKED"):
            await runtime.write_file(sandbox, "data", base64.b64encode(b"no").decode())
        for scope in (None, "task-a"):
            with (
                pytest.raises(RuntimeError, match="LOCKED"),
                runtime._execution_lock(sandbox, scope),
            ):
                pass
        destroys.append(asyncio.create_task(runtime.destroy("sandbox")))
        destroys.append(asyncio.create_task(runtime.destroy("sandbox")))
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in destroys)
        destroys[0].cancel()
        await asyncio.sleep(0)
        assert not destroys[0].done()
        assert sandbox.root.exists()
        with pytest.raises(RuntimeError, match="RELEASING"):
            await runtime.create("sandbox", 2, sandbox.uid)
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await build
        await asyncio.gather(*destroys, return_exceptions=True)
    assert not sandbox.root.exists()
    assert runtime.sandboxes == {}
    assert runtime._destroying == set()
    assert runtime._destroy_tasks == {}


async def test_cancelled_thread_exception_is_drained() -> None:
    entered = threading.Event()
    finish = threading.Event()

    def fail() -> None:
        entered.set()
        assert finish.wait(3)
        raise ValueError("worker failure")

    task = asyncio.create_task(complete_in_thread(fail))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_release_drains_cancelled_creation_before_removing_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = await runtime_and_sandbox(tmp_path)
    entered = threading.Event()
    finish = threading.Event()
    original_initialize = runtime_module._initialize_top_level

    def initialize_then_block(root: Path, uid: int, names: tuple[str, ...]) -> None:
        original_initialize(root, uid, names)
        entered.set()
        assert finish.wait(5), "test did not release the creation worker"

    monkeypatch.setattr(runtime_module, "_initialize_top_level", initialize_then_block)
    creation = asyncio.create_task(runtime.create("creating", 1, os.getuid()))
    destruction: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        creation.cancel()
        await asyncio.sleep(0)
        assert not creation.done()
        destruction = asyncio.create_task(runtime.destroy("creating"))
        await asyncio.sleep(0.05)
        assert not destruction.done()
        assert (runtime.settings.workspace_root / "creating").exists()
    finally:
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await creation
        if destruction is not None:
            await destruction
    assert "creating" not in runtime.sandboxes
    assert not (runtime.settings.workspace_root / "creating").exists()
    assert runtime._destroying == set()
    assert runtime._destroy_tasks == {}


@pytest.mark.parametrize("release_during_spawn", [False, True])
async def test_release_terminates_execution_before_draining_its_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_during_spawn: bool
) -> None:
    runtime, sandbox = await runtime_and_sandbox(tmp_path)
    runtime.settings.terminate_grace_seconds = 0.05
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    monkeypatch.setattr(runtime.builder, "build", lambda *_a, **_kw: command)
    original_spawn = asyncio.create_subprocess_exec
    entered = asyncio.Event()
    resume = asyncio.Event()
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)
        spawned.append(process)
        entered.set()
        if release_during_spawn:
            await resume.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    execution = asyncio.create_task(
        runtime.execute(
            sandbox,
            ExecRequest(exec_id="exec", generation=1, argv=["true"], exec_scope="task-a"),
        )
    )
    destruction: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        destruction = asyncio.create_task(runtime.destroy(sandbox.sandbox_id))
        if release_during_spawn:
            await asyncio.sleep(0.05)
            assert not destruction.done()
            resume.set()
        done, _ = await asyncio.wait({execution, destruction}, timeout=3)
        assert len(done) == 2, "release must not wait on a live execution's lock"
        await destruction
        if release_during_spawn:
            with pytest.raises(RuntimeError, match="RELEASING"):
                await execution
        else:
            assert (await execution).status == "FAILED"
        assert spawned[0].returncode is not None
        assert runtime.processes == {}
        assert runtime.sandboxes == {}
        assert not sandbox.root.exists()
        assert runtime._destroying == set()
        assert runtime._destroy_tasks == {}
    finally:
        resume.set()
        for process in spawned:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        if not execution.done():
            execution.cancel()
        await asyncio.gather(execution, return_exceptions=True)
        if destruction is not None:
            await destruction


async def test_strict_does_not_advertise_aggregate_quotas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = await runtime_and_sandbox(tmp_path)
    monkeypatch.setattr(runtime.builder, "probe", lambda: {})

    async def supported(*args: Any) -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(runtime, "_probe_candidate", supported)

    async def measured_launch(*args: Any) -> dict[str, object]:
        return {}

    monkeypatch.setattr("agent_sandbox.runtime.probe_toolchains", measured_launch)
    report = await runtime.probe()
    assert report["cgroup_namespace"] is True
    assert report["resource_control"] == {
        "process_limits": {
            "cpu_seconds_per_process": 300,
            "processes_per_real_uid": 64,
            "open_files_per_process": 512,
            "bytes_per_file": 1024 * 1024 * 1024,
        },
        "sandbox_cpu_quota": False,
        "sandbox_memory_quota": False,
        "sandbox_pid_quota": False,
        "sandbox_disk_quota": False,
        "execution_cgroup_supervision": False,
    }


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("template_snapshot_max_entries", 0),
        ("template_snapshot_max_depth", 257),
        ("template_snapshot_timeout_seconds", 0),
        ("template_snapshot_timeout_seconds", float("inf")),
    ],
)
def test_snapshot_settings_reject_invalid_limits(option: str, value: Any) -> None:
    with pytest.raises(ValueError):
        Settings(**{option: value})
