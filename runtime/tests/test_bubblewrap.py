import subprocess
import sys
from pathlib import Path

import pytest

from agent_sandbox_runtime import (
    PLATFORM_PYTHON_RUNTIME,
    PLATFORM_RUNTIME_PACKAGES,
    BubblewrapCommandBuilder,
    ExtensionHostCommandBuilder,
    InteractiveEnvironment,
    InteractiveSandboxCommandBuilder,
    IsolationLevel,
    SandboxMount,
    SandboxRuntimeError,
    SandboxRuntimeProfile,
    SandboxSymlink,
    runtime_mount,
)
from agent_sandbox_runtime.bubblewrap import SandboxCommand


def _profile() -> SandboxRuntimeProfile:
    return SandboxRuntimeProfile(
        bubblewrap_path=Path("/opt/bwrap"),
        setpriv_path=Path("/opt/setpriv"),
        prlimit_path=Path("/opt/prlimit"),
        readonly_mounts=("/usr",),
    )


def test_shared_runtime_profile_hash_is_upgraded() -> None:
    profile = SandboxRuntimeProfile()

    assert profile.profile_hash.endswith("-v12")
    assert "/opt/vendor" not in profile.readonly_mounts


def test_interactive_command_uses_persistent_workspace_and_managed_environment() -> None:
    command = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
            managed_environment=(
                ("DEPLOYMENT_ID", "production-1"),
                ("WORKLOAD_TOKEN", "test-token"),
            ),
            platform_python_runtime=Path("/platform/.venv"),
            platform_runtime_packages=Path("/platform/runtime"),
            extra_mounts=(
                SandboxMount(Path("/platform/config"), "/run/platform/config", "ro-bind-try"),
            ),
            extra_directories=("/run", "/run/platform", "/run/platform/config"),
        ),
    ).build(
        sandbox_root=Path("/sandboxes/s1"),
        sandbox_uid=20001,
        argv=("/bin/bash", "-lc", "python -V"),
        cwd="/workspace",
        user_env={"CASE_ID": "1"},
    )

    assert command[:2] == ["/opt/prlimit", "--cpu=300:300"]
    assert ["--bind", "/sandboxes/s1/workspace", "/workspace"] == command[
        command.index("--bind") : command.index("--bind") + 3
    ]
    assert "PIP_INDEX_URL" in command
    assert "UV_INDEX_URL" in command
    assert "https://pypi.example/simple" in command
    assert "UV_PYTHON_DOWNLOADS" in command
    assert "never" in command
    assert "UV_PROJECT_ENVIRONMENT" in command
    assert "/workspace/.venv" in command
    assert "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION" in command
    assert "python" in command
    assert "PYTHONDONTWRITEBYTECODE" in command
    assert "PYTHONNOUSERSITE" in command
    assert "PYTHONPATH" in command
    assert PLATFORM_RUNTIME_PACKAGES in command
    assert any(value.startswith(f"{PLATFORM_PYTHON_RUNTIME}/bin:") for value in command)
    assert "CASE_ID" in command
    deployment_index = command.index("DEPLOYMENT_ID")
    assert command[deployment_index - 1] == "--setenv"
    assert command[deployment_index + 1] == "production-1"
    token_index = command.index("WORKLOAD_TOKEN")
    assert command[token_index - 1] == "--setenv"
    assert command[token_index + 1] == "test-token"
    assert "--unshare-net" not in command
    config_index = command.index("/platform/config")
    assert command[config_index - 1] == "--ro-bind-try"
    assert command[config_index + 1] == "/run/platform/config"
    assert "--symlink" not in command
    platform_index = command.index("/platform/.venv")
    assert command[platform_index - 1] == "--ro-bind"
    assert command[platform_index + 1] == PLATFORM_PYTHON_RUNTIME
    packages_index = command.index("/platform/runtime")
    assert command[packages_index - 1] == "--ro-bind"
    assert command[packages_index + 1] == PLATFORM_RUNTIME_PACKAGES


def test_interactive_command_rejects_runtime_environment_override() -> None:
    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
        ),
    )
    with pytest.raises(ValueError, match="sandbox-managed"):
        builder.build(
            sandbox_root=Path("/sandboxes/s1"),
            sandbox_uid=20001,
            argv=("/bin/true",),
            cwd="/workspace",
            user_env={"PATH": "/tmp"},
        )


def test_interactive_command_rejects_managed_environment_override() -> None:
    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
            managed_environment=(("DEPLOYMENT_ID", "worker-service"),),
        ),
    )
    with pytest.raises(ValueError, match="sandbox-managed"):
        builder.build(
            sandbox_root=Path("/sandboxes/s1"),
            sandbox_uid=20001,
            argv=("/bin/true",),
            cwd="/workspace",
            user_env={"DEPLOYMENT_ID": "spoofed-service"},
        )


def test_interactive_command_accepts_arbitrary_managed_environment() -> None:
    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
            managed_environment=(("VENDOR_SPECIFIC_ID", "worker-credential"),),
        ),
    )
    command = builder.build(
        sandbox_root=Path("/sandboxes/s1"),
        sandbox_uid=20001,
        argv=("/bin/true",),
        cwd="/workspace",
        user_env={},
    )

    assert "VENDOR_SPECIFIC_ID" in command
    assert "worker-credential" in command


def test_interactive_command_rejects_duplicate_managed_environment() -> None:
    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
            managed_environment=(("DEPLOYMENT_ID", "one"), ("DEPLOYMENT_ID", "two")),
        ),
    )

    with pytest.raises(ValueError, match="unique"):
        builder.build(
            sandbox_root=Path("/sandboxes/s1"),
            sandbox_uid=20001,
            argv=("/bin/true",),
            cwd="/workspace",
            user_env={},
        )


def test_standard_isolation_adds_pid_namespace_and_private_procfs() -> None:
    profile = SandboxRuntimeProfile(
        bubblewrap_path=Path("/opt/bwrap"),
        setpriv_path=Path("/opt/setpriv"),
        prlimit_path=Path("/opt/prlimit"),
        readonly_mounts=("/usr",),
        isolation_level=IsolationLevel.STANDARD,
    )

    command = BubblewrapCommandBuilder(profile).build(
        SandboxCommand(argv=("/bin/true",), cwd="/", env={})
    )

    assert "--unshare-pid" in command
    assert command[command.index("--proc") : command.index("--proc") + 2] == [
        "--proc",
        "/proc",
    ]
    assert "--unshare-cgroup" not in command


@pytest.mark.parametrize(
    ("feature", "pid", "cgroup"),
    [("cgroup_namespace", False, True), ("pid_namespace", True, False)],
)
def test_basic_can_add_namespaces_without_changing_its_level(
    feature: str, pid: bool, cgroup: bool
) -> None:
    profile = SandboxRuntimeProfile(additional_features=(feature,))
    command = BubblewrapCommandBuilder(profile).build(
        SandboxCommand(argv=("/bin/true",), cwd="/", env={})
    )
    assert profile.isolation_level is IsolationLevel.BASIC
    assert ("--unshare-pid" in command) is pid
    assert ("--proc" in command) is pid
    assert ("--unshare-cgroup" in command) is cgroup
    assert profile.effective_profile_hash != SandboxRuntimeProfile().effective_profile_hash


def test_strict_isolation_adds_cgroup_namespace() -> None:
    profile = SandboxRuntimeProfile(
        bubblewrap_path=Path("/opt/bwrap"),
        setpriv_path=Path("/opt/setpriv"),
        prlimit_path=Path("/opt/prlimit"),
        readonly_mounts=("/usr",),
        isolation_level=IsolationLevel.STRICT,
    )

    command = BubblewrapCommandBuilder(profile).build(
        SandboxCommand(argv=("/bin/true",), cwd="/", env={})
    )

    assert "--unshare-pid" in command
    assert "--unshare-cgroup" in command


def test_nested_readonly_mount_creates_parents_and_is_readonly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        Path,
        "exists",
        lambda path: str(path) == "/opt/vendor/config",
    )
    command = BubblewrapCommandBuilder(
        SandboxRuntimeProfile(
            bubblewrap_path=Path("/opt/bwrap"),
            setpriv_path=Path("/opt/setpriv"),
            prlimit_path=Path("/opt/prlimit"),
            readonly_mounts=("/opt/vendor/config",),
        )
    ).build(SandboxCommand(argv=("/bin/true",), cwd="/", env={}))

    opt_index = command.index("/opt")
    data_index = command.index("/opt/vendor")
    mount_index = command.index("/opt/vendor/config")
    assert command[opt_index - 1] == "--dir"
    assert command[data_index - 1] == "--dir"
    assert command[mount_index - 1] == "--ro-bind"
    assert command[mount_index + 1] == "/opt/vendor/config"
    assert opt_index < data_index < mount_index


def test_missing_nested_readonly_mount_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "exists", lambda _path: False)
    command = BubblewrapCommandBuilder(
        SandboxRuntimeProfile(
            bubblewrap_path=Path("/opt/bwrap"),
            setpriv_path=Path("/opt/setpriv"),
            prlimit_path=Path("/opt/prlimit"),
            readonly_mounts=("/opt/vendor/config",),
        )
    ).build(SandboxCommand(argv=("/bin/true",), cwd="/", env={}))

    mount_index = command.index("/opt/vendor/config")
    assert command[mount_index - 1] == "--ro-bind-try"


def test_extension_host_is_network_isolated_and_read_only(tmp_path: Path) -> None:
    extension = tmp_path / "extension"
    runtime_dir = tmp_path / "rpc"
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    extension.mkdir()
    runtime_dir.mkdir()
    python = venv / "bin/python"
    python.touch()

    command = ExtensionHostCommandBuilder(_profile()).build(
        python_executable=str(python),
        runner_script="print('ready')",
        extension_root=str(extension),
        runtime_dir=str(runtime_dir),
    )

    assert "--unshare-net" in command
    pythonpath_index = command.index("PYTHONPATH")
    assert command[pythonpath_index - 1] == "--setenv"
    version_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    assert command[pythonpath_index + 1] == f"/runtime/lib/{version_dir}/site-packages"
    assert "PYTHONNOUSERSITE" in command
    extension_index = command.index(str(extension.resolve()))
    assert command[extension_index - 1] == "--ro-bind"
    assert command[extension_index + 1] == "/extension"
    assert command[-4:] == ["/runtime/bin/python", "-u", "-c", "print('ready')"]


def test_extension_host_mounts_venv_when_python_is_an_absolute_symlink(tmp_path: Path) -> None:
    system_python = tmp_path / "usr/local/bin/python3"
    system_python.parent.mkdir(parents=True)
    system_python.touch()
    venv = tmp_path / ".venv"
    python = venv / "bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(system_python)

    runtime_root, sandbox_python = runtime_mount(str(python))

    assert runtime_root == str(venv)
    assert sandbox_python == "/runtime/bin/python"


def test_extension_host_probe_runs_caller_runtime_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    venv = tmp_path / ".venv"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(BubblewrapCommandBuilder, "probe", lambda _self: {"profile_id": "test"})

    report = ExtensionHostCommandBuilder(_profile()).probe(
        python_executable=str(python),
        runner_script="import required_platform_module",
    )

    command = observed["command"]
    assert isinstance(command, list)
    assert command[-1] == "import required_platform_module"
    assert report["profile_id"] == "test"


def _interactive_builder() -> InteractiveSandboxCommandBuilder:
    return InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple",
            npm_registry="https://npm.example",
        ),
    )


def test_a_user_environment_with_a_malformed_name_is_rejected() -> None:
    """The pairs become `--setenv` argv, so a name is not free-form text."""

    with pytest.raises(ValueError, match="invalid environment variable name"):
        _interactive_builder().build(
            sandbox_root=Path("/sandboxes/s1"),
            sandbox_uid=20001,
            argv=("/bin/true",),
            cwd="/workspace",
            user_env={"1LEADING_DIGIT": "value"},
        )


def test_a_user_environment_value_containing_a_nul_is_rejected() -> None:
    """A NUL cannot cross `execve`, so it has to be refused before the argv."""

    with pytest.raises(ValueError, match="invalid environment variable value"):
        _interactive_builder().build(
            sandbox_root=Path("/sandboxes/s1"),
            sandbox_uid=20001,
            argv=("/bin/true",),
            cwd="/workspace",
            user_env={"SANDBOX_TEST": "before\x00after"},
        )


def test_a_python_runtime_outside_a_venv_or_usr_is_refused() -> None:
    """A runtime the mount logic cannot place is refused, not mounted wrong.

    `runtime_mount` has to find the interpreter's own package tree, and it
    knows only two shapes: a virtualenv, and the distribution's `/usr`.
    Anything else would land at a path whose site-packages is empty, which
    surfaces later as a ModuleNotFoundError in the sandbox instead of as a
    configuration error.
    """

    with pytest.raises(SandboxRuntimeError, match=r"must be inside a \.venv or /usr"):
        runtime_mount("/opt/somewhere/bin/python")


def test_a_usr_python_runtime_mounts_the_distribution_tree() -> None:
    """The second shape `runtime_mount` knows: a system interpreter.

    A container that installed its own Python has no virtualenv to mount, so
    the whole of `/usr` is the runtime and the interpreter keeps its own path
    inside the sandbox.
    """

    assert runtime_mount("/usr/lib/agent-sandbox-absent/bin/python") == (
        "/usr",
        "/usr/lib/agent-sandbox-absent/bin/python",
    )


def test_the_extension_socket_lands_in_the_mounted_rpc_directory() -> None:
    """`build` mounts the runtime directory at `rpc_path`.

    The client inside the sandbox connects to the path this returns, so a
    socket outside that mount would be one nothing can reach.
    """

    builder = ExtensionHostCommandBuilder(_profile())

    assert builder.socket_path("exec.sock") == f"{builder.rpc_path}/exec.sock"


def test_a_command_with_no_argv_is_refused() -> None:
    """Bubblewrap with no command starts a shell, which is not what was asked."""

    with pytest.raises(ValueError, match="argv cannot be empty"):
        BubblewrapCommandBuilder(_profile()).build(SandboxCommand(argv=(), cwd="/", env={}))


def test_a_symlink_is_reproduced_as_a_symlink() -> None:
    """Templates carry symlinks — a venv's `bin/python3` is one — so the built
    command has to recreate them rather than copy what they point at."""

    command = BubblewrapCommandBuilder(_profile()).build(
        SandboxCommand(
            argv=("/bin/true",),
            cwd="/workspace",
            env={},
            symlinks=(SandboxSymlink(source="/usr/bin/python3", target="/usr/bin/python"),),
        )
    )

    index = command.index("--symlink")
    assert command[index + 1 : index + 3] == ["/usr/bin/python3", "/usr/bin/python"]


def test_an_unreadable_existing_login_profile_is_left_alone(tmp_path: Path) -> None:
    """The login profile is an enhancement, so failing to read it must not
    fail the command the caller actually asked for."""

    sandbox_root = tmp_path / "sandbox"
    home = sandbox_root / "home"
    home.mkdir(parents=True)
    (home / ".profile").mkdir()

    _interactive_builder().ensure_login_path(sandbox_root=sandbox_root, sandbox_uid=20001)

    assert (home / ".profile").is_dir()


def test_a_failed_login_profile_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The profile is written beside its target and renamed into place.

    When the rename fails, the temporary has to go: a leftover
    `.profile.agent-sandbox` in a sandbox home would be inherited by the next
    sandbox that reused the directory, carrying a stale PATH.
    """

    sandbox_root = tmp_path / "sandbox"
    home = sandbox_root / "home"
    home.mkdir(parents=True)
    builder = _interactive_builder()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("agent_sandbox_runtime.bubblewrap.os.replace", fail)

    with pytest.raises(OSError, match="read-only"):
        builder.ensure_login_path(sandbox_root=sandbox_root, sandbox_uid=20001)

    assert list(home.iterdir()) == []
