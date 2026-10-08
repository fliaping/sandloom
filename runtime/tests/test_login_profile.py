"""The profile a sandbox's login shells read.

A login shell — `sh -lc`, `bash -lc`, the form most agents use — re-reads
/etc/profile, and the distribution's profile assigns PATH outright. Everything
the runtime composed is discarded, so the sandbox sees the stock system PATH and
answers "go: not found" for a toolchain the image installed and PATHed. The
shell reads this profile after /etc/profile, which is what makes the two forms
of the same command agree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from agent_sandbox_runtime import (
    InteractiveEnvironment,
    InteractiveSandboxCommandBuilder,
    SandboxRuntimeProfile,
    build_toolchain,
)
from agent_sandbox_runtime.bubblewrap import LOGIN_PROFILE_MARKER

# Every registry every builder might take; each one reads the keywords it
# declares and ignores the rest.
_TOOLCHAIN_OPTIONS = {
    "index_url": "https://pypi.example/simple/",
    "registry": "https://npm.example",
    "proxy": "https://goproxy.example",
    "registry_url": "https://crates.example",
    "maven_repository_url": "https://maven.example",
}


def _builder(*toolchains: str) -> InteractiveSandboxCommandBuilder:
    return InteractiveSandboxCommandBuilder(
        SandboxRuntimeProfile(
            bubblewrap_path=Path("/opt/bwrap"),
            setpriv_path=Path("/opt/setpriv"),
            prlimit_path=Path("/opt/prlimit"),
            readonly_mounts=("/usr",),
        ),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            toolchains=tuple(
                build_toolchain(name, **_TOOLCHAIN_OPTIONS) for name in toolchains
            ),
        ),
    )


def _sandbox(tmp_path: Path, uid: int = os.getuid()) -> Path:
    root = tmp_path / "sandbox"
    (root / "home").mkdir(parents=True)
    (root / "workspace").mkdir()
    return root


def test_a_login_shell_finds_the_toolchain_path(tmp_path: Path) -> None:
    """The property that matters: `sh -lc 'echo $PATH'` sees the toolchains.

    Asserted by running the built profile through a real shell rather than by
    matching its text, because what a shell does with the file is the contract.
    """
    builder = _builder("go", "rust")
    root = _sandbox(tmp_path)

    builder.ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    profile = root / "home" / ".profile"
    # Simulate what a login shell does after /etc/profile has clobbered PATH.
    script = f'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; . {profile}; echo "$PATH"'
    observed = subprocess.run(
        ["/bin/sh", "-c", script], capture_output=True, text=True, check=True
    ).stdout.strip()

    assert observed == builder.managed_path()
    for entry in ("/envs/go/bin", "/usr/local/go/bin", "/usr/local/cargo/bin"):
        assert entry in observed.split(":")


def test_the_profile_is_readable_by_the_sandbox_owner(tmp_path: Path) -> None:
    """A profile the sandbox cannot read would be a silent no-op."""
    root = _sandbox(tmp_path)

    _builder("go").ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    profile = root / "home" / ".profile"
    status = profile.stat()
    assert status.st_uid == os.getuid()
    assert status.st_mode & 0o400  # readable by its owner
    assert not status.st_mode & 0o022  # not group- or world-writable


def test_a_profile_the_owner_wrote_is_left_alone(tmp_path: Path) -> None:
    """It is their home. A template that ships a profile chose to."""
    root = _sandbox(tmp_path)
    profile = root / "home" / ".profile"
    profile.write_text("export MY_VAR=1\n", encoding="utf-8")

    _builder("go").ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    assert profile.read_text(encoding="utf-8") == "export MY_VAR=1\n"


def test_our_own_profile_is_refreshed_when_the_toolchains_change(tmp_path: Path) -> None:
    """A deployment that enables another language must not keep the old PATH."""
    root = _sandbox(tmp_path)

    _builder("go").ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())
    first = (root / "home" / ".profile").read_text(encoding="utf-8")
    assert "/envs/cargo/bin" not in first

    _builder("go", "rust").ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())
    second = (root / "home" / ".profile").read_text(encoding="utf-8")

    assert LOGIN_PROFILE_MARKER in second
    assert "/envs/cargo/bin" in second


def test_a_missing_home_directory_is_not_an_error(tmp_path: Path) -> None:
    """Building must not fail because a sandbox was prepared differently."""
    root = tmp_path / "sandbox"
    root.mkdir()

    _builder("go").ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    assert not (root / "home").exists()


def test_a_path_containing_a_quote_survives_the_shell(tmp_path: Path) -> None:
    """Paths are deployment-supplied, and the profile is shell source.

    A single quote in a path would otherwise end the quoting and leave the rest
    to be parsed as commands.
    """
    builder = InteractiveSandboxCommandBuilder(
        SandboxRuntimeProfile(
            bubblewrap_path=Path("/opt/bwrap"),
            setpriv_path=Path("/opt/setpriv"),
            prlimit_path=Path("/opt/prlimit"),
            readonly_mounts=("/usr",),
        ),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            toolchains=(
                build_toolchain("go", proxy="https://goproxy.example", install_root="/opt/it's here"),
            ),
        ),
    )
    root = _sandbox(tmp_path)

    builder.ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    profile = root / "home" / ".profile"
    observed = subprocess.run(
        ["/bin/sh", "-c", f'. "{profile}"; echo "$PATH"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert observed == builder.managed_path()
    assert "/opt/it's here/bin" in observed


@pytest.mark.parametrize("toolchains", [("python",), ("python", "node"), ("go", "rust", "java")])
def test_the_profile_always_matches_what_the_command_gets(
    tmp_path: Path, toolchains: tuple[str, ...]
) -> None:
    """One source of truth, so the two paths cannot drift."""
    builder = _builder(*toolchains)
    root = _sandbox(tmp_path)

    builder.ensure_login_path(sandbox_root=root, sandbox_uid=os.getuid())

    assert f"PATH='{builder.managed_path()}'" in (root / "home" / ".profile").read_text()


def test_asking_for_the_path_does_not_skip_a_proxy() -> None:
    """The accessor has to stay a read.

    It first asked `_managed_environment()` for the PATH, which rotates the
    configured proxies — so writing the login profile quietly consumed one, and
    a deployment with two proxies sent every command through the second.
    """

    builder = InteractiveSandboxCommandBuilder(
        SandboxRuntimeProfile(
            bubblewrap_path=Path("/opt/bwrap"),
            setpriv_path=Path("/opt/setpriv"),
            prlimit_path=Path("/opt/prlimit"),
            readonly_mounts=("/usr",),
        ),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            http_proxies=("proxy-a:8080", "proxy-b:8080"),
            toolchains=(build_toolchain("go", **_TOOLCHAIN_OPTIONS),),
        ),
    )

    builder.managed_path()
    built = builder.build(
        sandbox_root=Path("/tmp/sandbox"),
        sandbox_uid=os.getuid(),
        argv=("/bin/true",),
        cwd="/workspace",
        user_env={},
    )

    assert "http://proxy-a:8080" in built
