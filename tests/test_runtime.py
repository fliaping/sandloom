import os
import stat
import time
from pathlib import Path

import pytest
from agent_sandbox_runtime import SandboxMount

from agent_sandbox.config import Settings
from agent_sandbox.runtime import (
    BubblewrapCommandBuilder,
    LocalSandbox,
    RuntimeExtension,
    SandboxRuntime,
    _workspace_path,
)
from agent_sandbox.schemas import ExecRequest


def test_default_profile_is_portable() -> None:
    settings = Settings(internal_token="token")

    assert settings.readonly_mounts == ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    assert settings.profile_hash.endswith("-v12")


def test_command_builder_uses_allowlist_and_persistent_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOST_ONLY_SECRET", "must-not-enter-sandbox")
    root = tmp_path / "sb"
    for name in ("workspace", "home", "cache", "envs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    settings = Settings(
        internal_token="token",
        readonly_mounts=["/usr"],
        http_proxies=["10.0.0.9:11080"],
        python_index_url="https://pypi.example/simple/",
        npm_registry="https://npm.example",
    )
    extension = RuntimeExtension(
        managed_environment=(("DEPLOYMENT_ID", "production-1"),),
        mounts=(SandboxMount(Path("/platform/config"), "/run/platform/config", "ro-bind-try"),),
        directories=("/run", "/run/platform", "/run/platform/config"),
    )
    command = BubblewrapCommandBuilder(settings, extension=extension).build(
        LocalSandbox("sb-1", 3, 20001, root),
        ExecRequest(exec_id="exec-1", generation=3, argv=["/bin/echo", "ok"]),
    )

    assert "--ro-bind" in command
    assert ["--ro-bind", "/", "/"] != command
    assert "--unshare-pid" not in command
    assert "--proc" not in command
    assert "--overlay" not in command
    assert "--tmp-overlay" not in command
    assert "--ro-overlay" not in command
    assert "--disable-userns" in command
    assert str(root / "workspace") in command
    assert "http://10.0.0.9:11080" in command
    assert "/cache/npm" in command
    assert "https://npm.example" in command
    assert "https://pypi.example/simple/" in command
    assert "/cache/pnpm" in command
    assert "/cache/yarn" in command
    assert "/cache/uv" in command
    assert "UV_INDEX_URL" in command
    assert "UV_PYTHON_DOWNLOADS" in command
    assert "never" in command
    assert "UV_PROJECT_ENVIRONMENT" in command
    assert "/workspace/.venv" in command
    assert "DEPLOYMENT_ID" in command
    assert "production-1" in command
    assert "HOST_ONLY_SECRET" not in command
    assert "must-not-enter-sandbox" not in command
    assert "/platform/config" in command
    assert "/run/platform/config" in command
    assert "/opt/agent-sandbox/python-runtime" in command
    assert "/opt/agent-sandbox/runtime" in command
    assert "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION" in command
    assert "PYTHONDONTWRITEBYTECODE" in command
    assert "PYTHONNOUSERSITE" in command
    assert "/envs/npm-global" in command
    # Toolchain bin directories come before the system path, grouped by
    # language in the order the toolchains were composed.
    assert (
        "/opt/agent-sandbox/python-runtime/bin:/envs/uv-tools/bin:/envs/python-venv/bin:"
        "/envs/npm-global/bin:/envs/pnpm:"
        "/home/sandbox/.local/bin:/usr/local/bin:/usr/bin:/bin" in command
    )


def test_exec_cwd_must_stay_in_workspace() -> None:
    with pytest.raises(ValueError):
        ExecRequest(exec_id="exec-1", generation=1, argv=["true"], cwd="/etc")


def test_command_builder_can_degrade_without_disable_userns(tmp_path: Path) -> None:
    root = tmp_path / "sb"
    for name in ("workspace", "home", "cache", "envs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    settings = Settings(internal_token="token", readonly_mounts=["/usr"])

    command = BubblewrapCommandBuilder(settings, disable_nested_userns=False).build(
        LocalSandbox("sb-1", 1, 20001, root),
        ExecRequest(exec_id="exec-1", generation=1, argv=["/bin/true"]),
    )

    assert "--unshare-user" in command
    assert "--disable-userns" not in command


def test_workspace_path_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes the workspace"):
        _workspace_path(workspace, "/workspace/escape/secret.txt")


def test_exec_rejects_oversized_environment_value() -> None:
    with pytest.raises(ValueError, match="environment variable"):
        ExecRequest(
            exec_id="exec-1",
            generation=1,
            argv=["true"],
            env={"TOO_BIG": "x" * 65_537},
        )


def test_command_builder_rejects_reserved_environment(tmp_path: Path) -> None:
    root = tmp_path / "sb"
    for name in ("workspace", "home", "cache", "envs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    builder = BubblewrapCommandBuilder(Settings(internal_token="token", readonly_mounts=["/usr"]))

    with pytest.raises(ValueError, match="sandbox-managed"):
        builder.build(
            LocalSandbox("sb-1", 1, 20001, root),
            ExecRequest(
                exec_id="exec-1",
                generation=1,
                argv=["/bin/true"],
                env={"http_proxy": "http://untrusted-proxy"},
            ),
        )


def test_command_builder_rotates_configured_proxies(tmp_path: Path) -> None:
    root = tmp_path / "sb"
    for name in ("workspace", "home", "cache", "envs"):
        (root / name).mkdir(parents=True, exist_ok=True)
    builder = BubblewrapCommandBuilder(
        Settings(
            internal_token="token",
            readonly_mounts=["/usr"],
            http_proxies=["proxy-a:8080", "proxy-b:8080"],
        )
    )
    sandbox = LocalSandbox("sb-1", 1, 20001, root)
    request = ExecRequest(exec_id="exec-1", generation=1, argv=["/bin/true"])

    first = builder.build(sandbox, request)
    second = builder.build(sandbox, request)

    assert "http://proxy-a:8080" in first
    assert "http://proxy-b:8080" in second


async def test_existing_sandbox_does_not_recursively_chmod(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    settings = Settings(
        internal_token="token",
        local_root=tmp_path,
        min_free_bytes=0,
        disk_high_watermark_percent=99,
    )
    runtime = SandboxRuntime(settings)
    sandbox = await runtime.create("sb-1", 1, os.getuid())
    nested = sandbox.workspace / "node_modules"
    nested.mkdir()
    nested.chmod(0o755)
    runtime.sandboxes.clear()

    await runtime.create("sb-1", 2, os.getuid())

    assert stat.S_IMODE(nested.stat().st_mode) == 0o755


async def test_destroy_moves_large_tree_to_trash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    settings = Settings(
        internal_token="token",
        local_root=tmp_path,
        min_free_bytes=0,
        disk_high_watermark_percent=99,
    )
    runtime = SandboxRuntime(settings)
    sandbox = await runtime.create("sb-1", 1, os.getuid())
    (sandbox.workspace / "large-tree").mkdir()

    await runtime.destroy("sb-1")

    assert not sandbox.root.exists()
    assert any((tmp_path / ".trash").iterdir())
    await runtime.cleanup_trash()
    assert list((tmp_path / ".trash").iterdir()) == []


def test_idle_sandbox_candidates_exclude_running_process(tmp_path: Path) -> None:
    settings = Settings(internal_token="token", local_root=tmp_path, idle_ttl_seconds=10)
    runtime = SandboxRuntime(settings)
    sandbox = LocalSandbox("sb-1", 1, 20001, tmp_path / "sb-1")
    runtime.sandboxes[sandbox.sandbox_id] = sandbox
    runtime.last_active_at[sandbox.sandbox_id] = time.monotonic() - 11

    assert runtime.idle_sandboxes() == [sandbox]
    runtime.processes[(sandbox.sandbox_id, "exec-1")] = object()  # type: ignore[assignment]
    assert runtime.idle_sandboxes() == []
