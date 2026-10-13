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


@pytest.fixture(params=["auto", "off"], ids=["cgroup-if-available", "rlimit-supervisor"])
async def runtime(tmp_path: Path, request: pytest.FixtureRequest) -> Any:
    local_root = tmp_path / "sandboxes"
    local_root.mkdir()
    for ancestor in (local_root, *local_root.parents):
        os.chmod(ancestor, os.stat(ancestor).st_mode | 0o011)
        if ancestor == Path(tempfile.gettempdir()):
            break
    instance = SandboxRuntime(
        Settings(
            internal_token="integration-token",
            cgroup_mode=request.param,
            local_root=local_root,
            database_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
            uid_start=20100,
            uid_end=20199,
            cpu_quota=1,
            memory_limit_bytes=MEMORY_LIMIT,
            pids_limit=64,
            address_space_limit_bytes=2 * MEMORY_LIMIT,
            disk_limit_bytes=128 * 1024 * 1024,
            disk_check_interval_seconds=0.5,
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
    # Aggregate limits hold under both schemes; only how differs.
    assert control["sandbox_memory_quota"] is True
    assert control["sandbox_cpu_quota"] is True
    assert control["sandbox_pid_quota"] is True
    assert control["sandbox_disk_quota"] is True
    expected = "kernel" if cgroup else "supervised"
    assert control["enforcement"]["memory"].startswith(expected)
    assert control["enforcement"]["cpu"].startswith(expected)
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
    if result.status != "OOM_KILLED":
        # Without cgroups RLIMIT_AS may stop the single process first.
        assert _scheme(runtime) != "cgroup_v2"
        assert "MemoryError" in result.stderr
    else:
        assert result.error_code == "SANDBOX_MEMORY_LIMIT_EXCEEDED"
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


async def test_multi_process_memory_is_limited_in_aggregate(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-multimem", 1, 20105)
    # Four processes of 100 MiB: each under every per-process limit.
    result = await _run(
        runtime,
        sandbox,
        "python3 - <<'P'\nimport multiprocessing as m, time\n"
        "def f():\n  b=bytearray(100<<20)\n  b[::4096]=b'1'*len(b[::4096])\n  time.sleep(20)\n"
        "ps=[m.Process(target=f) for _ in range(4)]\n[p.start() for p in ps]\n"
        "[p.join() for p in ps]\nP",
        limit=60,
    )
    assert result.status == "OOM_KILLED", (result.status, result.stderr[-300:])
    assert result.error_code == "SANDBOX_MEMORY_LIMIT_EXCEEDED"


async def test_disk_quota_stops_a_runaway_writer(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-disk", 1, 20106)
    result = await _run(
        runtime,
        sandbox,
        "for i in $(seq 1 64); do head -c 16777216 /dev/zero > /workspace/f$i; done; sleep 10",
        limit=60,
    )
    assert result.error_code == "SANDBOX_DISK_QUOTA_EXCEEDED"
    assert (await _run(runtime, sandbox, "rm -f /workspace/f*; echo ok")).stdout.strip() == "ok"


async def test_cpu_quota_caps_a_multicore_spin(runtime: SandboxRuntime) -> None:
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
    # Four spinners for 3 s wall under a one-core quota: about 3 s of CPU,
    # plus up to one CPU-second of burst when supervised.
    assert float(result.stdout.strip().splitlines()[-1]) < 3 * 1.5 + 1
