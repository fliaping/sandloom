"""Regression tests for live, attacker-controlled workspace trees."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import signal
import sys
from pathlib import Path

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.runtime import LocalSandbox, SandboxRuntime
from agent_sandbox.schemas import ExecRequest


@pytest.fixture(autouse=True)
def _allow_chown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chown", lambda *_a, **_kw: None)


def _runtime(tmp_path: Path) -> tuple[SandboxRuntime, LocalSandbox]:
    root = tmp_path / "sandbox"
    (root / "workspace").mkdir(parents=True)
    settings = Settings(internal_token="token", local_root=tmp_path, terminate_grace_seconds=0.05)
    runtime = SandboxRuntime(settings)
    sandbox = LocalSandbox("sandbox", 1, os.getuid(), root)
    runtime.sandboxes[sandbox.sandbox_id] = sandbox
    return runtime, sandbox


@pytest.mark.parametrize("operation", ["read", "list", "delete", "move"])
async def test_directory_replacement_cannot_redirect_file_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    directory = sandbox.workspace / "live"
    directory.mkdir()
    (directory / "data").write_text("tenant data")
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "data").write_text("host secret")
    original_lock = runtime._path_lock
    swapped = False

    @contextlib.contextmanager
    def replace_at_lock(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal swapped
        with original_lock(*args, **kwargs):  # type: ignore[arg-type]
            if not swapped:
                directory.rename(sandbox.workspace / "old")
                directory.symlink_to(outside, target_is_directory=True)
                swapped = True
            yield

    monkeypatch.setattr(runtime, "_path_lock", replace_at_lock)
    try:
        if operation == "read":
            result = base64.b64decode(await runtime.read_file(sandbox, "live/data"))
            assert result != b"host secret"
        elif operation == "list":
            entries, _, _ = await runtime.list_directory(sandbox, "live", limit=10, offset=0)
            assert all(entry["size_bytes"] != len("host secret") for entry in entries)
        elif operation == "delete":
            await runtime.delete_path(sandbox, "live/data", recursive=False)
        else:
            await runtime.move_path(sandbox, "live/data", "moved", overwrite=True)
    except ValueError:
        pass  # Refusing a tree that changed is a safe outcome.
    assert (outside / "data").read_text() == "host secret"
    assert not (sandbox.workspace / "moved").exists()


async def test_write_cannot_follow_parent_replaced_after_permission_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    directory = sandbox.workspace / "live"
    directory.mkdir()
    outside = tmp_path / "host"
    outside.mkdir()
    original_write = Path.write_bytes

    def replace_before_write(path: Path, data: bytes) -> int:
        if path.parent == directory:
            directory.rename(sandbox.workspace / "old")
            directory.symlink_to(outside, target_is_directory=True)
        return original_write(path, data)

    monkeypatch.setattr(Path, "write_bytes", replace_before_write)
    await runtime.write_file(sandbox, "live/data", base64.b64encode(b"tenant data").decode())
    assert not (outside / "data").exists()
    assert list(outside.iterdir()) == []


async def test_timeout_kills_descendant_after_command_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    command = [
        sys.executable,
        "-c",
        "import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
        "'import time,signal; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)']); print('started', flush=True)",
    ]
    monkeypatch.setattr(runtime.builder, "build", lambda *_a, **_kw: command)
    original_spawn = asyncio.create_subprocess_exec
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(*args: object, **kwargs: object) -> asyncio.subprocess.Process:
        process = await original_spawn(*args, **kwargs)  # type: ignore[arg-type]
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(
        runtime._execute_locked(
            sandbox, ExecRequest(exec_id="exec", generation=1, argv=["true"], timeout_seconds=1)
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=3)
        assert done, "timeout must cover descendants holding the output pipes open"
        result = task.result()
        assert result.status == "TIMED_OUT"
        assert "started" in result.stdout
        assert runtime.processes == {}
    finally:
        for process in spawned:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
        await asyncio.wait({task}, timeout=2)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "list", "delete", "move"])
async def test_open_directory_stays_pinned_when_agent_replaces_its_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    directory = sandbox.workspace / "live"
    directory.mkdir()
    (directory / "data").write_text("tenant data")
    outside = tmp_path / "host"
    outside.mkdir()
    (outside / "data").write_text("host secret")
    original_open = os.open
    replaced = False

    def open_and_replace(
        path: object, flags: int, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> int:
        nonlocal replaced
        fd = original_open(path, flags, mode, dir_fd=dir_fd)  # type: ignore[arg-type]
        if path == "live" and dir_fd is not None and not replaced:
            directory.rename(sandbox.workspace / "old")
            directory.symlink_to(outside, target_is_directory=True)
            replaced = True
        return fd

    monkeypatch.setattr(os, "open", open_and_replace)
    if operation == "read":
        assert base64.b64decode(await runtime.read_file(sandbox, "live/data")) == b"tenant data"
    elif operation == "write":
        await runtime.write_file(sandbox, "live/data", base64.b64encode(b"updated").decode())
        assert (sandbox.workspace / "old/data").read_bytes() == b"updated"
    elif operation == "mkdir":
        await runtime.make_directory(sandbox, "live/new", parents=False)
        assert (sandbox.workspace / "old/new").is_dir()
    elif operation == "list":
        entries, _, _ = await runtime.list_directory(sandbox, "live", limit=10, offset=0)
        assert entries[0]["size_bytes"] == len("tenant data")
    elif operation == "delete":
        await runtime.delete_path(sandbox, "live/data", recursive=False)
        assert not (sandbox.workspace / "old/data").exists()
    else:
        await runtime.move_path(sandbox, "live/data", "moved", overwrite=False)
        assert (sandbox.workspace / "moved").read_text() == "tenant data"
    assert replaced
    assert (outside / "data").read_text() == "host secret"
    assert not (outside / "new").exists()


async def test_read_refuses_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    runtime, sandbox = _runtime(tmp_path)
    os.mkfifo(sandbox.workspace / "pipe")
    with pytest.raises(ValueError, match="regular file"):
        await runtime.read_file(sandbox, "pipe")


async def test_read_is_bounded_even_if_file_grows_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    runtime.settings.max_file_api_bytes = 16
    target = sandbox.workspace / "data"
    target.write_bytes(b"small")
    original_fstat = os.fstat

    def stat_and_grow(fd: int) -> os.stat_result:
        info = original_fstat(fd)
        target.write_bytes(b"x" * 10_000)
        return info

    monkeypatch.setattr(os, "fstat", stat_and_grow)
    with pytest.raises(ValueError, match="size limit"):
        await runtime.read_file(sandbox, "data")


@pytest.mark.parametrize("operation", ["read", "write", "mkdir", "list"])
async def test_file_api_refuses_even_in_workspace_symlink_traversal(
    tmp_path: Path, operation: str
) -> None:
    runtime, sandbox = _runtime(tmp_path)
    (sandbox.workspace / "real").mkdir()
    (sandbox.workspace / "real/data").write_text("data")
    (sandbox.workspace / "link").symlink_to("real", target_is_directory=True)
    with pytest.raises(ValueError):
        if operation == "read":
            await runtime.read_file(sandbox, "link/data")
        elif operation == "write":
            await runtime.write_file(sandbox, "link/new", base64.b64encode(b"data").decode())
        elif operation == "mkdir":
            await runtime.make_directory(sandbox, "link/new", parents=True)
        else:
            await runtime.list_directory(sandbox, "link", limit=10, offset=0)
    assert not (sandbox.workspace / "real/new").exists()


async def test_move_onto_ancestor_does_not_delete_source_tree(tmp_path: Path) -> None:
    runtime, sandbox = _runtime(tmp_path)
    (sandbox.workspace / "tree/child").mkdir(parents=True)
    (sandbox.workspace / "tree/child/keep").write_text("data")
    with pytest.raises(ValueError, match="overlapping"):
        await runtime.move_path(sandbox, "tree/child", "tree", overwrite=True)
    assert (sandbox.workspace / "tree/child/keep").read_text() == "data"


async def test_path_lock_fences_aliases_of_the_same_target(tmp_path: Path) -> None:
    runtime, sandbox = _runtime(tmp_path)
    with runtime._path_lock(sandbox, "/workspace/dir/./data"):
        with pytest.raises(RuntimeError, match="FILE_PATH_LOCKED"):
            await runtime.write_file(sandbox, "dir/data", base64.b64encode(b"data").decode())


@pytest.mark.parametrize("field", ["egress_denied_addresses", "egress_allowed_literals"])
def test_builtin_backend_refuses_unenforced_egress_policies(field: str) -> None:
    settings = Settings(internal_token="token", **{field: ["127.0.0.1"]})
    with pytest.raises(ValueError, match="EGRESS_POLICY_UNSUPPORTED"):
        SandboxRuntime(settings)
