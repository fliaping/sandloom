"""Bubblewrap isolation verified by running real commands inside a Linux container.

These tests only run where Bubblewrap can actually create namespaces, which means
a Linux host and, for the UID-drop assertions, root. Everywhere else they skip.
Run them with:

    ./scripts/integration-test.sh sandbox

The point is to prove the boundary empirically: a sandbox must not read another
sandbox's files, must not regain privileges, and must observe the namespaces the
negotiated level claims.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.runtime import SandboxRuntime
from agent_sandbox.schemas import ExecRequest

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap requires Linux"),
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap is not installed on this host"),
]

requires_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="dropping to a sandbox UID requires root"
)


@pytest.fixture
async def runtime(tmp_path: Path) -> Any:
    """A probed runtime, so `builder` reflects what this kernel actually permits."""
    local_root = tmp_path / "sandboxes"
    local_root.mkdir()
    # bwrap resolves the workspace path *after* setpriv drops to the sandbox UID,
    # so every ancestor must be traversable by that UID. A real deployment gets
    # this from /var/lib/agent-sandbox (0755); pytest's tmp_path base is 0700.
    for ancestor in (local_root, *local_root.parents):
        os.chmod(ancestor, os.stat(ancestor).st_mode | 0o011)
        if ancestor == Path(tempfile.gettempdir()):
            break

    settings = Settings(
        internal_token="integration-token",
        local_root=local_root,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
        isolation_level="auto",
        network_mode="host",
        uid_start=20000,
        uid_end=20099,
    )
    instance = SandboxRuntime(settings)
    report = await instance.probe()
    # Expose the probe result so tests can assert against the negotiated level.
    instance.probe_report = report  # type: ignore[attr-defined]
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _run(runtime: SandboxRuntime, sandbox: Any, argv: list[str], **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "exec_id": f"exec-{argv[0].replace('/', '-')}-{os.urandom(4).hex()}",
        "generation": sandbox.generation,
        "argv": argv,
        "timeout_seconds": 30,
    }
    options.update(overrides)
    return await runtime.execute(sandbox, ExecRequest(**options))


async def test_probe_reports_a_negotiated_level(runtime: SandboxRuntime) -> None:
    report = runtime.probe_report  # type: ignore[attr-defined]

    assert report["selected_level"] in {"basic", "standard", "strict"}
    assert report["supported_levels"]
    # The probe must have run a real command, not inferred from the kernel version.
    assert report["private_tmp"] is True


@requires_root
async def test_command_runs_as_the_sandbox_uid(runtime: SandboxRuntime) -> None:
    """The host-visible UID is what isolates tenants, not the in-namespace one.

    bwrap maps the sandbox to uid 0 inside its own user namespace, so `id -u`
    and /proc/self/uid_map both report 0 — they are re-translated into the
    reader's namespace. The only observation that proves the boundary is what
    the host kernel records as the owner of a file the sandbox created.
    """
    sandbox = await runtime.create("sandbox-uid", 1, 20001)

    result = await _run(runtime, sandbox, ["/bin/bash", "-c", "touch /workspace/owned.txt"])

    assert result.exit_code == 0
    assert os.stat(sandbox.workspace / "owned.txt").st_uid == 20001


@requires_root
async def test_workspace_is_writable_by_the_sandbox(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-write", 1, 20002)

    result = await _run(
        runtime,
        sandbox,
        ["/bin/bash", "-c", "echo content > /workspace/file.txt && cat /workspace/file.txt"],
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == "content"


@requires_root
async def test_one_sandbox_cannot_read_another_workspace(runtime: SandboxRuntime) -> None:
    """The core multi-tenancy claim, proven by attempting the read."""
    victim = await runtime.create("sandbox-victim", 1, 20003)
    await _run(
        runtime,
        victim,
        ["/bin/bash", "-c", "echo victim-secret > /workspace/secret.txt"],
    )
    attacker = await runtime.create("sandbox-attacker", 1, 20004)

    # The victim workspace is not mounted in the attacker's namespace at all.
    listing = await _run(runtime, attacker, ["/bin/bash", "-c", "ls /workspace"])
    assert "secret.txt" not in listing.stdout

    # Even the host path must be unreachable from inside the sandbox.
    escape = await _run(
        runtime,
        attacker,
        ["/bin/bash", "-c", f"cat {victim.workspace}/secret.txt 2>&1 || true"],
    )
    assert "victim-secret" not in escape.stdout


@requires_root
async def test_setuid_binaries_cannot_regain_privilege(runtime: SandboxRuntime) -> None:
    """`no_new_privs` must be set, so a setuid binary gains nothing."""
    sandbox = await runtime.create("sandbox-nnp", 1, 20005)

    result = await _run(
        runtime,
        sandbox,
        ["/bin/bash", "-c", "grep NoNewPrivs /proc/self/status"],
    )

    assert result.exit_code == 0
    assert "1" in result.stdout.split(":")[-1]


@requires_root
async def test_tmp_is_private_per_sandbox(runtime: SandboxRuntime) -> None:
    first = await runtime.create("sandbox-tmp-a", 1, 20006)
    second = await runtime.create("sandbox-tmp-b", 1, 20007)

    await _run(runtime, first, ["/bin/bash", "-c", "echo leak > /tmp/marker"])
    result = await _run(runtime, second, ["/bin/bash", "-c", "ls /tmp"])

    assert "marker" not in result.stdout


@requires_root
async def test_system_mounts_are_read_only(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-ro", 1, 20008)

    result = await _run(
        runtime,
        sandbox,
        ["/bin/bash", "-c", "touch /usr/bin/injected 2>&1 || echo blocked"],
    )

    assert "blocked" in result.stdout or result.exit_code != 0


@requires_root
async def test_pid_namespace_hides_host_processes(runtime: SandboxRuntime) -> None:
    """Only meaningful at `standard` and above."""
    report = runtime.probe_report  # type: ignore[attr-defined]
    if not report["pid_namespace"]:
        pytest.skip(f"level {report['selected_level']} does not provide a PID namespace")
    sandbox = await runtime.create("sandbox-pid", 1, 20009)

    result = await _run(runtime, sandbox, ["/bin/bash", "-c", "ls /proc | grep -c '^[0-9]'"])

    assert result.exit_code == 0
    # A fresh PID namespace shows only the sandbox's own handful of processes.
    assert int(result.stdout.strip()) < 20


@requires_root
async def test_process_limit_is_enforced(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-nproc", 1, 20010)

    result = await _run(runtime, sandbox, ["/bin/bash", "-c", "ulimit -u"])

    assert result.exit_code == 0
    assert int(result.stdout.strip()) <= runtime.settings.max_processes


@requires_root
async def test_file_size_limit_is_enforced(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-fsize", 1, 20011)

    result = await _run(runtime, sandbox, ["/bin/bash", "-c", "ulimit -f"])

    assert result.exit_code == 0
    reported = result.stdout.strip()
    if reported != "unlimited":
        # ulimit -f reports 512-byte blocks.
        assert int(reported) * 512 <= runtime.settings.max_file_size_bytes


@requires_root
async def test_output_is_truncated_at_the_limit(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-output", 1, 20012)

    result = await _run(
        runtime,
        sandbox,
        ["/bin/bash", "-c", "yes abcdefghij | head -c 20000000"],
        timeout_seconds=60,
    )

    assert result.truncated is True
    assert len(result.stdout.encode()) <= runtime.settings.max_output_bytes + 4096


@requires_root
async def test_timeout_terminates_a_runaway_command(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-timeout", 1, 20013)

    result = await _run(runtime, sandbox, ["/bin/sleep", "30"], timeout_seconds=2)

    assert result.status == "TIMED_OUT"
    assert result.exit_code != 0


@requires_root
async def test_scoped_executions_run_concurrently(runtime: SandboxRuntime) -> None:
    """Two scopes in one sandbox must overlap in wall-clock time."""
    sandbox = await runtime.create("sandbox-parallel", 1, 20014)

    started = asyncio.get_running_loop().time()
    results = await asyncio.gather(
        _run(
            runtime,
            sandbox,
            ["/bin/sleep", "2"],
            exec_id="exec-scope-a",
            exec_scope="thread-a",
        ),
        _run(
            runtime,
            sandbox,
            ["/bin/sleep", "2"],
            exec_id="exec-scope-b",
            exec_scope="thread-b",
        ),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert all(item.exit_code == 0 for item in results)
    # Serialized execution would take at least 4s.
    assert elapsed < 3.5, f"scoped executions serialized: {elapsed:.2f}s"


@requires_root
async def test_same_scope_is_rejected_by_the_worker_lock(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-scope-conflict", 1, 20015)

    async def run_scoped(exec_id: str) -> Any:
        try:
            return await _run(
                runtime,
                sandbox,
                ["/bin/sleep", "2"],
                exec_id=exec_id,
                exec_scope="thread-a",
            )
        except RuntimeError as exc:
            return str(exc)

    outcomes = await asyncio.gather(run_scoped("exec-a"), run_scoped("exec-b"))

    codes = [item for item in outcomes if isinstance(item, str)]
    assert "SANDBOX_EXEC_SCOPE_LOCKED" in codes


@requires_root
async def test_file_api_writes_atomically(runtime: SandboxRuntime) -> None:
    import base64

    sandbox = await runtime.create("sandbox-file-api", 1, 20016)
    payload = base64.b64encode(b"file-api-content").decode()

    await runtime.write_file(sandbox, "/workspace/data.txt", payload)
    encoded = await runtime.read_file(sandbox, "/workspace/data.txt")

    assert base64.b64decode(encoded) == b"file-api-content"
    # No temporary file may be left behind by an atomic rename.
    leftovers = list((sandbox.workspace).glob(".*.tmp-*"))
    assert leftovers == []


@requires_root
async def test_path_traversal_is_rejected(runtime: SandboxRuntime) -> None:
    import base64

    sandbox = await runtime.create("sandbox-traversal", 1, 20017)

    with pytest.raises((ValueError, RuntimeError)):
        await runtime.write_file(
            sandbox, "/workspace/../../etc/passwd", base64.b64encode(b"x").decode()
        )


@requires_root
async def test_destroy_removes_the_workspace(runtime: SandboxRuntime) -> None:
    sandbox = await runtime.create("sandbox-destroy", 1, 20018)
    await _run(runtime, sandbox, ["/bin/bash", "-c", "echo data > /workspace/file.txt"])
    root = sandbox.root

    await runtime.destroy("sandbox-destroy")

    assert not (root / "workspace" / "file.txt").exists()
