"""Worker-side locking for scoped parallel execution and file API calls."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.runtime import LocalSandbox, SandboxRuntime
from agent_sandbox.schemas import ExecRequest, ExecResponse


@pytest.fixture(autouse=True)
def _allow_chown_without_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file API chowns to the sandbox UID, which needs root on a real worker."""
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)


def _runtime_and_sandbox(tmp_path: Path) -> tuple[SandboxRuntime, LocalSandbox]:
    runtime = SandboxRuntime(Settings(internal_token="token", local_root=tmp_path))
    root = tmp_path / "sb"
    root.mkdir()
    (root / "workspace").mkdir()
    sandbox = LocalSandbox("sb-1", 1, os.getuid(), root)
    runtime.sandboxes[sandbox.sandbox_id] = sandbox
    return runtime, sandbox


def _request(exec_id: str, scope: str | None) -> ExecRequest:
    return ExecRequest(exec_id=exec_id, generation=1, argv=["true"], exec_scope=scope)


async def test_distinct_scopes_execute_concurrently(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    active = 0
    max_active = 0

    async def execute_locked(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.03)
        active -= 1
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = execute_locked  # type: ignore[method-assign]

    await asyncio.gather(
        runtime.execute(sandbox, _request("exec-1", "thread-1")),
        runtime.execute(sandbox, _request("exec-2", "thread-2")),
    )

    assert max_active == 2


async def test_same_scope_and_lifecycle_commands_are_fenced(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute_locked(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        entered.set()
        await release.wait()
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = execute_locked  # type: ignore[method-assign]
    first = asyncio.create_task(runtime.execute(sandbox, _request("exec-1", "thread-1")))
    await entered.wait()
    try:
        # The same scope is already busy.
        with pytest.raises(RuntimeError, match="SANDBOX_EXEC_SCOPE_LOCKED"):
            await runtime.execute(sandbox, _request("exec-2", "thread-1"))
        # An unscoped lifecycle command needs the global lock exclusively.
        with pytest.raises(RuntimeError, match="SANDBOX_SHARED_WORKSPACE_LOCKED"):
            await runtime.execute(sandbox, _request("exec-3", None))
    finally:
        release.set()
        await first


async def test_scope_lock_is_released_after_a_failed_execution(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)

    async def failing(_sandbox: LocalSandbox, _request: ExecRequest) -> ExecResponse:
        raise RuntimeError("BOOM")

    runtime._execute_locked = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="BOOM"):
        await runtime.execute(sandbox, _request("exec-1", "thread-1"))

    async def succeeding(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = succeeding  # type: ignore[method-assign]
    result = await runtime.execute(sandbox, _request("exec-2", "thread-1"))
    assert result.status == "SUCCEEDED"


async def test_file_writes_do_not_block_a_running_scoped_execution(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute_locked(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        entered.set()
        await release.wait()
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = execute_locked  # type: ignore[method-assign]
    running = asyncio.create_task(runtime.execute(sandbox, _request("exec-1", "thread-1")))
    await entered.wait()
    try:
        await runtime.write_file(sandbox, "notes.txt", base64.b64encode(b"hello").decode())
        assert base64.b64decode(await runtime.read_file(sandbox, "notes.txt")) == b"hello"
    finally:
        release.set()
        await running


async def test_file_writes_are_blocked_by_a_lifecycle_command(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def execute_locked(_sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        entered.set()
        await release.wait()
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED")

    runtime._execute_locked = execute_locked  # type: ignore[method-assign]
    running = asyncio.create_task(runtime.execute(sandbox, _request("exec-1", None)))
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="SANDBOX_FILE_PATH_LOCKED"):
            await runtime.write_file(sandbox, "notes.txt", base64.b64encode(b"hi").decode())
    finally:
        release.set()
        await running


async def test_write_file_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    await runtime.write_file(sandbox, "dir/notes.txt", base64.b64encode(b"first").decode())
    await runtime.write_file(sandbox, "dir/notes.txt", base64.b64encode(b"second").decode())

    target = sandbox.workspace / "dir" / "notes.txt"
    assert target.read_bytes() == b"second"
    assert [item.name for item in target.parent.iterdir()] == ["notes.txt"]


async def test_concurrent_writes_to_one_path_are_serialized(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    await runtime.write_file(sandbox, "notes.txt", base64.b64encode(b"first").decode())

    # A second writer holding the same path lock must be rejected rather than
    # interleaving a partial write.
    with runtime._path_lock(sandbox, "notes.txt"):
        with pytest.raises(RuntimeError, match="SANDBOX_FILE_PATH_LOCKED"):
            await runtime.write_file(sandbox, "notes.txt", base64.b64encode(b"second").decode())
    assert (sandbox.workspace / "notes.txt").read_bytes() == b"first"


async def test_different_paths_are_written_concurrently(tmp_path: Path) -> None:
    runtime, sandbox = _runtime_and_sandbox(tmp_path)
    with runtime._path_lock(sandbox, "a.txt"):
        await runtime.write_file(sandbox, "b.txt", base64.b64encode(b"ok").decode())
    assert (sandbox.workspace / "b.txt").read_bytes() == b"ok"
