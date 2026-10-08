"""What the runtime refuses to start on.

`BubblewrapCommandBuilder.probe()` is the gate between a host and a running
service. It decides whether Bubblewrap is present, whether it is setuid, and
whether it is new enough; its return value becomes the capabilities the service
advertises and the profile hash replicas compare before they place work on each
other's workers.

Its success path runs against a real Linux kernel in the container suite. Every
one of its refusals used to run for the first time in front of an operator whose
deployment had just failed — the worst moment to find out that the message does
not say which binary, or which version, was wrong.

`negotiate_isolation` has the same shape: the branch that fires when no level can
be provided at all is the one that means "this host cannot run sandboxes", and
nothing had ever reached it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from agent_sandbox_runtime import (
    BubblewrapCommandBuilder,
    ExtensionHostCommandBuilder,
    IsolationLevel,
    SandboxRuntimeError,
    SandboxRuntimeProfile,
    negotiate_isolation,
)

MINIMUM = "0.11.2"


def _script(path: Path, body: str) -> Path:
    """A real, runnable file standing in for a runtime binary."""

    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def host(tmp_path: Path) -> SandboxRuntimeProfile:
    """A host that would be accepted: three runnable binaries, a good version."""

    return SandboxRuntimeProfile(
        bubblewrap_path=_script(tmp_path / "bwrap", f'echo "bubblewrap {MINIMUM}"'),
        prlimit_path=_script(tmp_path / "prlimit", "exit 0"),
        setpriv_path=_script(tmp_path / "setpriv", "exit 0"),
    )


def test_a_host_that_satisfies_the_profile_is_accepted(host: SandboxRuntimeProfile) -> None:
    report = BubblewrapCommandBuilder(host).probe()

    assert report["bubblewrap_version"] == MINIMUM
    assert report["profile_id"] == host.profile_id
    # The hash other replicas compare against is the effective one, not the bare
    # profile_hash: two replicas at different isolation levels run different
    # sandboxes and must not be treated as interchangeable.
    assert report["profile_hash"] == host.effective_profile_hash
    assert report["profile_hash"].endswith("-basic-net-host")
    assert (report["user_namespace"], report["mount_namespace"]) == (True, True)


@pytest.mark.parametrize(
    ("attribute", "label"),
    [
        ("bubblewrap_path", "Bubblewrap"),
        ("prlimit_path", "prlimit"),
        ("setpriv_path", "setpriv"),
    ],
)
def test_a_missing_runtime_binary_is_refused_by_name(
    host: SandboxRuntimeProfile, tmp_path: Path, attribute: str, label: str
) -> None:
    """The message has to name which of the three is missing, and where it looked."""

    absent = tmp_path / "absent"
    with pytest.raises(SandboxRuntimeError) as caught:
        BubblewrapCommandBuilder(replace(host, **{attribute: absent})).probe()

    assert label in str(caught.value)
    assert str(absent) in str(caught.value)


def test_a_runtime_binary_without_the_executable_bit_is_refused(
    host: SandboxRuntimeProfile, tmp_path: Path
) -> None:
    """Existing is not the same as runnable, and the check is not `exists()`."""

    present = tmp_path / "bwrap-not-executable"
    present.write_text("#!/bin/sh\n")
    present.chmod(0o644)

    with pytest.raises(SandboxRuntimeError, match="not executable"):
        BubblewrapCommandBuilder(replace(host, bubblewrap_path=present)).probe()


def test_setuid_bubblewrap_is_refused_without_being_run(tmp_path: Path) -> None:
    """A setuid binary is a privilege boundary no sandbox can be built on.

    The refusal has to come from the file's mode, before the binary runs: asking
    a setuid program its version is already the escalation.
    """

    marker = tmp_path / "executed"
    bwrap = _script(tmp_path / "bwrap", f'touch "{marker}"; echo "bubblewrap {MINIMUM}"')
    bwrap.chmod(0o4755)

    with pytest.raises(SandboxRuntimeError, match="setuid"):
        BubblewrapCommandBuilder(
            SandboxRuntimeProfile(
                bubblewrap_path=bwrap,
                prlimit_path=_script(tmp_path / "prlimit", "exit 0"),
                setpriv_path=_script(tmp_path / "setpriv", "exit 0"),
            )
        ).probe()

    assert not marker.exists(), "the setuid binary was executed"


def test_a_bubblewrap_that_fails_its_version_command_reports_stderr(
    host: SandboxRuntimeProfile,
) -> None:
    """The operator needs Bubblewrap's own words, not just "the probe failed"."""

    _script(host.bubblewrap_path, 'echo "bwrap: unrecognized option --version" >&2; exit 1')

    with pytest.raises(SandboxRuntimeError, match="unrecognized option"):
        BubblewrapCommandBuilder(host).probe()


def test_a_version_that_cannot_be_read_is_refused(host: SandboxRuntimeProfile) -> None:
    _script(host.bubblewrap_path, 'echo "bubblewrap (development build)"')

    with pytest.raises(SandboxRuntimeError, match="cannot parse"):
        BubblewrapCommandBuilder(host).probe()


def test_a_bubblewrap_below_the_minimum_is_refused(host: SandboxRuntimeProfile) -> None:
    _script(host.bubblewrap_path, 'echo "bubblewrap 0.10.0"')

    with pytest.raises(SandboxRuntimeError, match=f"below required {MINIMUM}"):
        BubblewrapCommandBuilder(host).probe()


def test_the_version_gate_compares_numbers_and_not_text(host: SandboxRuntimeProfile) -> None:
    """0.11.10 is newer than 0.11.2; comparing them as text says the opposite.

    This gate is what stands between an old Bubblewrap and a sandbox weaker than
    its profile claims, so it has to be right at the digit where the two
    orderings disagree.
    """

    _script(host.bubblewrap_path, 'echo "bubblewrap 0.11.10"')

    assert BubblewrapCommandBuilder(host).probe()["bubblewrap_version"] == "0.11.10"


def test_a_network_isolated_replica_has_a_different_profile_hash(
    host: SandboxRuntimeProfile,
) -> None:
    """Egress policy is part of the profile, so placement cannot mix the two."""

    assert host.effective_profile_hash.endswith("-net-host")
    assert replace(host, network_isolated=True).effective_profile_hash.endswith("-net-none")


def test_an_already_effective_hash_is_not_suffixed_again() -> None:
    """Applying the suffix twice would make one sandbox look like two profiles."""

    once = SandboxRuntimeProfile().effective_profile_hash

    assert replace(SandboxRuntimeProfile(), profile_hash=once).effective_profile_hash == once


def test_a_probe_that_fails_inside_the_sandbox_reports_why(tmp_path: Path) -> None:
    """The probe runs a real command; its stderr is the whole diagnosis.

    This is what catches an editable install whose ``.pth`` target sits outside
    the sandbox mounts — the failure is a missing module, and a message that
    dropped the subprocess output would leave nothing to act on.
    """

    # The wrapper passes its arguments through to the command after `--`, the way
    # the real prlimit does, so the failure reported is the inner one.
    prlimit = _script(
        tmp_path / "prlimit", 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"'
    )
    _script(
        tmp_path / "bwrap", 'echo "ModuleNotFoundError: No module named platform_pkg" >&2; exit 1'
    )

    venv = tmp_path / ".venv"
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()

    profile = SandboxRuntimeProfile(
        bubblewrap_path=tmp_path / "bwrap",
        prlimit_path=prlimit,
        # Root callers use setpriv too. Forward through it, otherwise a
        # successful stub hides the inner failure until the version probe.
        setpriv_path=_script(
            tmp_path / "setpriv", 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"'
        ),
    )
    with pytest.raises(SandboxRuntimeError) as caught:
        ExtensionHostCommandBuilder(profile).probe(python_executable=str(python))

    message = str(caught.value)
    assert "extension profile probe failed" in message
    assert "platform_pkg" in message


def test_a_host_that_provides_no_isolation_level_is_refused_with_the_reasons() -> None:
    """`auto` falls back to a weaker level; it does not fall back to none."""

    def probe(level: IsolationLevel) -> tuple[bool, str]:
        return False, f"no user namespace at {level.label}"

    with pytest.raises(RuntimeError) as caught:
        negotiate_isolation("auto", probe)

    message = str(caught.value)
    assert "no supported Bubblewrap isolation level" in message
    for label in ("basic", "standard", "strict"):
        assert f"{label}=" in message


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_id", "   "),
        ("profile_hash", ""),
        ("root_drop_uid", 0),
        ("tmpfs_bytes", 0),
        ("max_open_files", 0),
        ("max_file_size_bytes", 0),
        ("cpu_seconds", 0),
        ("max_processes", 0),
    ],
)
def test_a_profile_with_an_impossible_value_is_rejected(field: str, value: object) -> None:
    """Each of these is a hole in the boundary rather than a preference.

    A limit of zero is not "unlimited" — it is a sandbox that cannot start, or
    one whose uid drop has been configured away.
    """

    with pytest.raises(ValueError, match=field):
        replace(SandboxRuntimeProfile(), **{field: value}).validate()
