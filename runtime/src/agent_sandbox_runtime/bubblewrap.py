"""Shared Bubblewrap command construction and host probing."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal

from .profile import SandboxRuntimeProfile
from .toolchains import (
    Toolchain,
    compose_environment,
    node_toolchain,
    python_toolchain,
    reserved_environment,
)

MountMode = Literal["bind", "ro-bind", "ro-bind-try"]

_RESERVED_ENV_NAMES = {
    "BASH_ENV",
    "ENV",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "NO_PROXY",
    "PATH",
    "TEMP",
    "TMP",
    "TMPDIR",
    "XDG_CACHE_HOME",
}
_RESERVED_ENV_PREFIXES = ("LD_",)
_BASE_PATH = ("/home/sandbox/.local/bin", "/usr/local/bin", "/usr/bin", "/bin")
# A login shell — `sh -lc`, `bash -lc`, which is how most agents run a command —
# re-reads /etc/profile, and the distribution's profile assigns PATH outright:
#
#     if [ "$(id -u)" -eq 0 ]; then PATH="/usr/local/sbin:..."; else PATH=...; fi
#
# That assignment discards everything the runtime composed, so a login shell
# sees Go, Rust, and the per-sandbox toolchain directories disappear from PATH
# and answers "go: not found" for a toolchain the image installed. The shell
# reads this file after /etc/profile, so writing the composed PATH here makes
# the two forms agree.
LOGIN_PROFILE = ".profile"
LOGIN_PROFILE_MARKER = "# managed by agent-sandbox: restores PATH after /etc/profile"
PLATFORM_PYTHON_RUNTIME = "/opt/agent-sandbox/python-runtime"
PLATFORM_RUNTIME_PACKAGES = "/opt/agent-sandbox/runtime"


class SandboxRuntimeError(RuntimeError):
    """The shared runtime cannot satisfy its declared isolation profile."""


@dataclass(frozen=True, slots=True)
class SandboxMount:
    source: Path
    target: str
    mode: MountMode = "bind"


@dataclass(frozen=True, slots=True)
class SandboxSymlink:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class SandboxCommand:
    argv: tuple[str, ...]
    cwd: str
    env: Mapping[str, str]
    mounts: tuple[SandboxMount, ...] = ()
    directories: tuple[str, ...] = ()
    symlinks: tuple[SandboxSymlink, ...] = ()
    outer_uid: int | None = None
    unshare_network: bool = False


@dataclass(frozen=True, slots=True)
class InteractiveEnvironment:
    """The managed environment every execution in a sandbox receives.

    ``toolchains`` decides which languages are configured. It defaults to
    Python and Node built from ``python_index_url`` / ``npm_registry`` so an
    existing deployment keeps its environment unchanged; pass the tuple
    explicitly to add Go, Rust, or the JVM.
    """

    python_index_url: str
    npm_registry: str
    no_proxy: str = "127.0.0.1,localhost,::1"
    http_proxies: tuple[str, ...] = ()
    managed_environment: tuple[tuple[str, str], ...] = ()
    platform_python_runtime: Path | None = None
    platform_runtime_packages: Path | None = None
    extra_mounts: tuple[SandboxMount, ...] = ()
    extra_directories: tuple[str, ...] = ()
    toolchains: tuple[Toolchain, ...] | None = None

    def resolved_toolchains(self) -> tuple[Toolchain, ...]:
        if self.toolchains is not None:
            return self.toolchains
        return (
            python_toolchain(
                index_url=self.python_index_url,
                platform_python_runtime=PLATFORM_PYTHON_RUNTIME
                if self.platform_python_runtime is not None
                else None,
                platform_runtime_packages=PLATFORM_RUNTIME_PACKAGES
                if self.platform_runtime_packages is not None
                else None,
            ),
            node_toolchain(registry=self.npm_registry),
        )


class BubblewrapCommandBuilder:
    """Build one Bubblewrap process from a versioned runtime profile."""

    def __init__(self, profile: SandboxRuntimeProfile) -> None:
        profile.validate()
        self.profile = profile

    def build(self, command: SandboxCommand) -> list[str]:
        if not command.argv:
            raise ValueError("Sandbox command argv cannot be empty")
        _validate_environment(command.env)
        profile = self.profile
        args = [str(profile.prlimit_path)]
        if profile.cpu_seconds is not None:
            args.append(f"--cpu={profile.cpu_seconds}:{profile.cpu_seconds}")
        if profile.max_processes is not None:
            args.append(f"--nproc={profile.max_processes}:{profile.max_processes}")
        args.extend(
            [
                f"--nofile={profile.max_open_files}:{profile.max_open_files}",
                f"--fsize={profile.max_file_size_bytes}:{profile.max_file_size_bytes}",
                "--core=0:0",
                "--",
            ]
        )
        if command.outer_uid is not None:
            args.extend(
                [
                    str(profile.setpriv_path),
                    "--reuid",
                    str(command.outer_uid),
                    "--regid",
                    str(command.outer_uid),
                    "--clear-groups",
                    "--no-new-privs",
                    "--",
                ]
            )
        args.extend([str(profile.bubblewrap_path), "--unshare-user"])
        if profile.disable_nested_userns:
            args.append("--disable-userns")
        args.extend(["--uid", "0", "--gid", "0", "--unshare-ipc", "--unshare-uts"])
        if "pid_namespace" in profile.features:
            args.append("--unshare-pid")
        if "cgroup_namespace" in profile.features:
            args.append("--unshare-cgroup")
        if command.unshare_network or profile.network_isolated:
            args.append("--unshare-net")
        args.extend(["--die-with-parent", "--new-session", "--clearenv"])
        for directory in _mount_parent_directories(profile.readonly_mounts):
            args.extend(["--dir", directory])
        for system_mount in profile.readonly_mounts:
            flag = "--ro-bind" if Path(system_mount).exists() else "--ro-bind-try"
            args.extend([flag, system_mount, system_mount])
        args.extend(
            [
                "--dev",
                "/dev",
                "--perms",
                "0700",
                "--size",
                str(profile.tmpfs_bytes),
                "--tmpfs",
                "/tmp",
            ]
        )
        if "private_procfs" in profile.features:
            args.extend(["--proc", "/proc"])
        for directory in command.directories:
            args.extend(["--dir", directory])
        for bind_mount in command.mounts:
            args.extend(
                [
                    f"--{bind_mount.mode}",
                    str(bind_mount.source.resolve()),
                    bind_mount.target,
                ]
            )
        for symlink in command.symlinks:
            args.extend(["--symlink", symlink.source, symlink.target])
        args.extend(["--chdir", command.cwd])
        for key, value in command.env.items():
            args.extend(["--setenv", key, value])
        return [*args, "--", *command.argv]

    def probe(self) -> dict[str, object]:
        profile = self.profile
        for label, path in (
            ("Bubblewrap", profile.bubblewrap_path),
            ("prlimit", profile.prlimit_path),
            ("setpriv", profile.setpriv_path),
        ):
            if not path.is_file() or not os.access(path, os.X_OK):
                raise SandboxRuntimeError(f"{label} is not executable: {path}")
        if profile.bubblewrap_path.stat().st_mode & stat.S_ISUID:
            raise SandboxRuntimeError("setuid Bubblewrap is forbidden")
        result = subprocess.run(
            [str(profile.bubblewrap_path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise SandboxRuntimeError(result.stderr.strip() or "bwrap --version failed")
        match = re.search(r"\d+(?:\.\d+)+", result.stdout)
        if match is None:
            raise SandboxRuntimeError(f"cannot parse Bubblewrap version: {result.stdout!r}")
        version = match.group(0)
        if _version_tuple(version) < _version_tuple(profile.minimum_version):
            raise SandboxRuntimeError(
                f"Bubblewrap {version} is below required {profile.minimum_version}"
            )
        return {
            "profile_id": profile.profile_id,
            "profile_hash": profile.effective_profile_hash,
            "bubblewrap_version": version,
            "user_namespace": True,
            "mount_namespace": True,
            "ipc_namespace": True,
            "uts_namespace": True,
            "private_tmp": True,
        }


def _mount_parent_directories(mounts: Sequence[str]) -> tuple[str, ...]:
    directories: set[PurePath] = set()
    for mount in mounts:
        path = PurePath(mount)
        directories.update(parent for parent in path.parents if str(parent) != "/")
    return tuple(
        str(path) for path in sorted(directories, key=lambda item: (len(item.parts), str(item)))
    )


def _shell_quote(value: str) -> str:
    """Single-quote a value for `sh`, which has no escape inside single quotes."""

    return "'" + value.replace("'", "'\\''") + "'"


class InteractiveSandboxCommandBuilder:
    """Build commands for a persistent Agent development workspace."""

    def __init__(
        self,
        profile: SandboxRuntimeProfile,
        environment: InteractiveEnvironment,
    ) -> None:
        self.profile = profile
        self.environment = environment
        self._builder = BubblewrapCommandBuilder(profile)
        self._proxy_index = 0

    def build(
        self,
        *,
        sandbox_root: Path,
        sandbox_uid: int,
        argv: Sequence[str],
        cwd: str,
        user_env: Mapping[str, str],
        extra_mounts: Sequence[SandboxMount] = (),
    ) -> list[str]:
        managed_names = {name.upper() for name, _ in self.environment.managed_environment}
        toolchains = self.environment.resolved_toolchains()
        reserved_names, reserved_prefixes = reserved_environment(toolchains)
        _validate_user_environment(
            user_env,
            reserved_names=managed_names | set(reserved_names),
            reserved_prefixes=reserved_prefixes,
        )
        env = self._managed_environment()
        env.update(user_env)
        mounts = tuple(
            SandboxMount(sandbox_root / name, target)
            for name, target in (
                ("workspace", "/workspace"),
                ("home", "/home/sandbox"),
                ("cache", "/cache"),
                ("envs", "/envs"),
            )
        )
        if self.environment.platform_python_runtime is not None:
            mounts += (
                SandboxMount(
                    self.environment.platform_python_runtime,
                    PLATFORM_PYTHON_RUNTIME,
                    "ro-bind",
                ),
            )
        if self.environment.platform_runtime_packages is not None:
            mounts += (
                SandboxMount(
                    self.environment.platform_runtime_packages,
                    PLATFORM_RUNTIME_PACKAGES,
                    "ro-bind",
                ),
            )
        mounts += self.environment.extra_mounts
        # Per-execution mounts come last so a resolved environment template can
        # land on /envs/<name> without the deployment-wide mounts shadowing it.
        mounts += tuple(extra_mounts)
        return self._builder.build(
            SandboxCommand(
                argv=tuple(argv),
                cwd=cwd,
                env=env,
                mounts=mounts,
                directories=(
                    "/workspace",
                    "/home",
                    "/home/sandbox",
                    "/cache",
                    "/envs",
                    "/opt/agent-sandbox",
                    PLATFORM_PYTHON_RUNTIME,
                    PLATFORM_RUNTIME_PACKAGES,
                    *self.environment.extra_directories,
                    *(mount.target for mount in extra_mounts),
                ),
                outer_uid=sandbox_uid,
            )
        )

    def managed_path(self) -> str:
        """The PATH this builder gives every command it runs.

        It does not assemble the whole environment: that rotates the configured
        proxies, so merely asking what the path is would skip one.
        """
        _, path_entries = self._composed_environment()
        return ":".join(path_entries)

    def ensure_login_path(self, *, sandbox_root: Path, sandbox_uid: int) -> None:
        """Teach the sandbox's login shells the composed PATH.

        Called before a command is built, because a shell that reads
        /etc/profile loses the PATH from the environment; see LOGIN_PROFILE.
        A profile the sandbox owner wrote is left alone — it is their home, and
        a template that ships one is making a deliberate choice.
        """

        home = sandbox_root / "home"
        if not home.is_dir():
            return
        profile = home / LOGIN_PROFILE
        body = (
            f"{LOGIN_PROFILE_MARKER}\n"
            "# /etc/profile assigns PATH outright, which drops the language\n"
            "# toolchains the sandbox was given. This restores them.\n"
            f"PATH={_shell_quote(self.managed_path())}\n"
            "export PATH\n"
        )
        try:
            if profile.exists() and LOGIN_PROFILE_MARKER not in profile.read_text(
                encoding="utf-8", errors="replace"
            )[:400]:
                return
        except OSError:
            return
        # Write beside the target and rename, so a shell reading the profile
        # concurrently never sees a half-written PATH.
        temporary = home / f".{LOGIN_PROFILE}.agent-sandbox"
        temporary.write_text(body, encoding="utf-8")
        try:
            # Only root can hand a file to another uid, and only root needs to:
            # a service that is not root runs its sandboxes under its own uid,
            # and would have failed to prepare the sandbox directory at all if
            # the uid were someone else's.
            if os.geteuid() == 0 and sandbox_uid != 0:
                os.chown(temporary, sandbox_uid, sandbox_uid)
            os.chmod(temporary, 0o644)
            os.replace(temporary, profile)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _composed_environment(self) -> tuple[dict[str, str], tuple[str, ...]]:
        """The toolchain variables and PATH, without the per-command parts.

        Split out so a caller that only needs the PATH does not also advance the
        proxy rotation, which `_managed_environment` does.
        """

        return compose_environment(
            self.environment.resolved_toolchains(), base_path=_BASE_PATH
        )

    def _managed_environment(self) -> dict[str, str]:
        environment = self.environment
        managed_environment = dict(environment.managed_environment)
        if len(managed_environment) != len(environment.managed_environment):
            raise ValueError("managed environment variable names must be unique")
        _validate_environment(managed_environment)
        toolchain_env, path_entries = self._composed_environment()
        return {
            "HOME": "/home/sandbox",
            "TMPDIR": "/tmp",
            "TMP": "/tmp",
            "TEMP": "/tmp",
            "XDG_CACHE_HOME": "/cache",
            **toolchain_env,
            "PATH": ":".join(path_entries),
            "LANG": "C.UTF-8",
            **managed_environment,
            **self._proxy_environment(),
        }

    def _proxy_environment(self) -> dict[str, str]:
        proxies = self.environment.http_proxies
        if not proxies:
            return {}
        proxy = proxies[self._proxy_index % len(proxies)]
        self._proxy_index += 1
        if "://" not in proxy:
            proxy = f"http://{proxy}"
        return {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
            "NO_PROXY": self.environment.no_proxy,
            "no_proxy": self.environment.no_proxy,
        }


class ExtensionHostCommandBuilder:
    """Build a long-lived, network-isolated ExtensionHost process."""

    extension_path = "/extension"
    rpc_path = "/rpc"

    def __init__(self, profile: SandboxRuntimeProfile) -> None:
        self.profile = profile
        self._builder = BubblewrapCommandBuilder(profile)

    def prepare_runtime_dir(self, runtime_dir: str) -> None:
        path = Path(runtime_dir)
        if os.geteuid() == 0:
            os.chown(path, self.profile.root_drop_uid, self.profile.root_drop_uid)
        path.chmod(0o700)

    def socket_path(self, socket_name: str) -> str:
        return str(Path(self.rpc_path) / socket_name)

    def build(
        self,
        *,
        python_executable: str,
        runner_script: str,
        extension_root: str,
        runtime_dir: str,
    ) -> list[str]:
        runtime_root, sandbox_python = runtime_mount(python_executable)
        outer_uid = self.profile.root_drop_uid if os.geteuid() == 0 else None
        environment = {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PATH": "/runtime/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        }
        if runtime_root != "/usr":
            # Do not rely on CPython discovering pyvenv.cfg through an absolute
            # interpreter symlink. The mounted venv is the authoritative runtime.
            environment["PYTHONPATH"] = _sandbox_site_packages_path()
        return self._builder.build(
            SandboxCommand(
                argv=(sandbox_python, "-u", "-c", runner_script),
                cwd=self.extension_path,
                env=environment,
                mounts=(
                    SandboxMount(Path(runtime_root), "/runtime", "ro-bind"),
                    SandboxMount(Path(extension_root), self.extension_path, "ro-bind"),
                    SandboxMount(Path(runtime_dir), self.rpc_path),
                ),
                outer_uid=outer_uid,
                unshare_network=True,
            )
        )

    def probe(
        self,
        *,
        python_executable: str,
        runner_script: str = "import sys; sys.exit(0)",
    ) -> dict[str, object]:
        """Run a real ExtensionHost command inside the sandbox profile.

        Callers may provide an import probe for platform modules that must be
        available from the mounted Python runtime. This catches editable
        installs whose ``.pth`` target exists outside the sandbox mounts.
        """
        runtime_root, _ = runtime_mount(python_executable)
        with (
            tempfile.TemporaryDirectory(prefix="agent-sandbox-extension-probe-") as extension_root,
            tempfile.TemporaryDirectory(prefix="agent-sandbox-rpc-probe-") as runtime_dir,
        ):
            Path(extension_root).chmod(0o555)
            self.prepare_runtime_dir(runtime_dir)
            command = self.build(
                python_executable=python_executable,
                runner_script=runner_script,
                extension_root=extension_root,
                runtime_dir=runtime_dir,
            )
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        if result.returncode != 0:
            raise SandboxRuntimeError(
                "Bubblewrap extension profile probe failed: " + result.stderr.strip()[:1000]
            )
        return {
            **self._builder.probe(),
            "mode": "required",
            "network_namespace": True,
            "runtime_root": runtime_root,
        }


def runtime_mount(python_executable: str) -> tuple[str, str]:
    executable = Path(python_executable)
    lexical = executable.absolute() if executable.is_absolute() else Path(sys.executable).absolute()
    if lexical.parent.name == "bin" and lexical.parent.parent.name == ".venv":
        # Keep the venv path before resolving its Python symlink. Container venvs
        # commonly point at /usr/local/bin/python; resolving first would mount
        # only /usr and silently hide every package installed in the venv.
        runtime_root = lexical.parent.parent
        relative = lexical.relative_to(runtime_root)
        return str(runtime_root), str(Path("/runtime") / relative)
    resolved = lexical.resolve()
    if str(resolved).startswith("/usr/"):
        return "/usr", str(resolved)
    raise SandboxRuntimeError(f"Python runtime must be inside a .venv or /usr: {python_executable}")


def _sandbox_site_packages_path() -> str:
    version_dir = f"python{sys.version_info.major}.{sys.version_info.minor}"
    return str(Path("/runtime/lib") / version_dir / "site-packages")


def _validate_environment(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"invalid environment variable value: {key!r}")


def _validate_user_environment(
    env: Mapping[str, str],
    *,
    reserved_names: set[str] | None = None,
    reserved_prefixes: Sequence[str] = (),
) -> None:
    reserved = _RESERVED_ENV_NAMES | (reserved_names or set())
    prefixes = _RESERVED_ENV_PREFIXES + tuple(reserved_prefixes)
    for key in env:
        normalized = key.upper()
        if normalized in reserved or normalized.startswith(prefixes):
            raise ValueError(
                f"this environment variable is sandbox-managed and cannot be overridden: {key}"
            )
    _validate_environment(env)


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


__all__ = [
    "PLATFORM_PYTHON_RUNTIME",
    "PLATFORM_RUNTIME_PACKAGES",
    "BubblewrapCommandBuilder",
    "ExtensionHostCommandBuilder",
    "InteractiveEnvironment",
    "InteractiveSandboxCommandBuilder",
    "SandboxCommand",
    "SandboxMount",
    "SandboxRuntimeError",
    "runtime_mount",
]
