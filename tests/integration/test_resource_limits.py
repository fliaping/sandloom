"""Resource limits, verified with real runaway commands in the worker image.

The scheme depends on the container: with a manageable cgroup v2 subtree the
sandbox gets aggregate cpu/memory/pids limits, otherwise per-process rlimits.
Each test asserts the behaviour of whichever scheme `/healthz` reports, so a
report that claims a quota it does not enforce fails here.

    ./scripts/integration-test.sh sandbox
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.runtime import SandboxRuntime
from agent_sandbox.schemas import ExecRequest

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap requires Linux"),
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap is not installed"),
    pytest.mark.skipif(os.geteuid() != 0, reason="sandbox UIDs and cgroups need root"),
]

MEMORY_LIMIT = 256 * 1024 * 1024


@pytest.fixture
async def runtime(tmp_path: Path) -> Any:
    local_root = tmp_path / "sandboxes"
    local_root.mkdir()
    for ancestor in (local_root, *local_root.parents):
        os.chmod(ancestor, os.stat(ancestor).st_mode | 0o011)
        if ancestor == Path(tempfile.gettempdir()):
            break
    instance = SandboxRuntime(
        Settings(
            internal_token="integration-token",
            local_root=local_root,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
            uid_start=20100,
            uid_end=20199,
            cpu_quota=1,
            memory_limit_bytes=MEMORY_LIMIT,
            pids_limit=64,
            address_space_limit_bytes=2 * MEMORY_LIMIT,
            min_free_bytes=0,
            disk_high_watermark_percent=99,
        )
    )
    report = await instance.probe()
    instance.probe_report = report  # type: ignore[attr-defined]
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _run(runtime: SandboxRuntime, sandbox: Any, script: str, limit: int = 30) -> Any:
    return await runtime.execute(
        sandbox,
        ExecRequest(
            exec_id=f"exec-{os.urandom(4).hex()}",
            generation=sandbox.generation,
            argv=["/bin/bash", "-c", script],
            timeout_seconds=limit,
        ),
    )


def _scheme(runtime: SandboxRuntime) -> str:
    return runtime.probe_report["resource_control"]["scheme"]  # type: ignore[attr-defined]


async def test_report_matches_the_active_scheme(runtime: SandboxRuntime) -> None:
    control = runtime.probe_report["resource_control"]  # type: ignore[attr-defined]
    cgroup = control["scheme"] == "cgroup_v2"
    assert control["sandbox_memory_quota"] is cgroup
    assert control["sandbox_cpu_quota"] is cgroup
    assert control["sandbox_pid_quota"] is cgroup
    if not cgroup:
        assert runtime.probe_report["selected_level"] != "strict"  # type: ignore[attr-defined]
        assert control["limits"]["address_space_bytes_per_process"] == 2 * MEMORY_LIMIT


async def test_memory_bomb_is_stopped(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-membomb", 1, 20101)
    result = await _run(
        runtime,
        sandbox,
        "python3 -c 'b=[]\nwhile True: b.append(bytearray(16*1024*1024))'",
    )
    if _scheme(runtime) == "cgroup_v2":
        assert result.status == "OOM_KILLED"
        assert result.error_code == "SANDBOX_MEMORY_LIMIT_EXCEEDED"
    else:
        assert result.status == "FAILED"
        assert "MemoryError" in result.stderr
    # The sandbox is usable afterwards.
    assert (await _run(runtime, sandbox, "echo ok")).stdout.strip() == "ok"


async def test_fork_bomb_is_bounded_and_leaves_nothing(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-forkbomb", 1, 20102)
    other = await runtime.create("sandbox-neighbour", 1, 20103)
    started = time.monotonic()
    await _run(runtime, sandbox, ":(){ :|:& };: ; sleep 3", limit=6)
    assert time.monotonic() - started < 30
    assert (await _run(runtime, other, "echo alive")).stdout.strip() == "alive"
    # Survivors would hold the sandbox's process budget; none may remain.
    assert (await _run(runtime, sandbox, "echo again")).stdout.strip() == "again"


async def test_cpu_quota_caps_a_multicore_spin(runtime: SandboxRuntime) -> None:
    if _scheme(runtime) != "cgroup_v2":
        pytest.skip("no CPU quota without cgroups; RLIMIT_CPU is per process")
    sandbox = await runtime.create("sandbox-spin", 1, 20104)
    result = await _run(
        runtime,
        sandbox,
        "python3 - <<'P'\nimport multiprocessing as m, os, time\n"
        "def f():\n  t=time.time()\n  while time.time()-t<3: pass\n"
        "ps=[m.Process(target=f) for _ in range(4)]\n[p.start() for p in ps]\n"
        "[p.join() for p in ps]\nc=os.times()\nprint(c.children_user+c.children_system)\nP",
        limit=60,
    )
    assert result.status == "SUCCEEDED", result.stderr
    # Four spinners for 3 s wall under a one-core quota: about 3 s of CPU.
    assert float(result.stdout.strip().splitlines()[-1]) < 3 * 1.5
