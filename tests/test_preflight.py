from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from agent_sandbox import preflight
from agent_sandbox.config import Settings
from agent_sandbox.preflight import (
    check_user_namespace_support,
    toolchain_isolation_warnings,
    validate_runtime_environment,
)


@pytest.fixture(autouse=True)
def host_allows_user_namespaces(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Make the preflight tests independent of the kernel they run on.

    `validate_runtime_environment` also reports the host's user-namespace
    settings, so on a machine with `kernel.apparmor_restrict_unprivileged_userns=1`
    (GitHub's hosted runners, Ubuntu 24.04) every exact-list assertion below
    gained a warning that has nothing to do with what it tests. The probe has its
    own tests, which opt out of this fixture.
    """
    if request.node.get_closest_marker("real_host_probe"):
        return
    sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
    sysctl.write_text("0\n", encoding="utf-8")
    monkeypatch.setattr(preflight, "_APPARMOR_USERNS_SYSCTL", sysctl)
    monkeypatch.setattr(preflight, "_has_bounding_capability", lambda _capability: True)


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    values: dict[str, object] = {
        "internal_token": "test-token",
        "advertise_host": "10.20.30.40",
        "local_root": tmp_path / "sandboxes",
        "bubblewrap_path": _executable(binaries / "bwrap"),
        "setpriv_path": _executable(binaries / "setpriv"),
        "prlimit_path": _executable(binaries / "prlimit"),
        "bash_path": _executable(binaries / "bash"),
    }
    values.update(overrides)
    return Settings(**values)


def test_staging_preflight_accepts_routable_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "staging")
    settings = _settings(tmp_path)
    settings.workspace_root.mkdir()

    warnings = validate_runtime_environment(settings)

    assert warnings == []


def test_s3_warns_when_using_implicit_credential_chain(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "staging")
    settings = _settings(
        tmp_path,
        blobstore_endpoint="http://object-store.internal",
        blobstore_bucket="sandbox",
    )

    assert validate_runtime_environment(settings) == [
        "BlobStore has an endpoint and bucket but no explicit credentials; "
        "falling back to the standard boto3 credential chain"
    ]


def test_staging_preflight_rejects_loopback_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "staging")
    settings = _settings(tmp_path, advertise_host="127.0.0.1")

    with pytest.raises(RuntimeError, match="loopback address"):
        validate_runtime_environment(settings)


def test_preflight_requires_heartbeat_safety_margin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "local")
    settings = _settings(
        tmp_path,
        heartbeat_interval_seconds=10,
        heartbeat_ttl_seconds=20,
    )

    with pytest.raises(RuntimeError, match="heartbeat TTL"):
        validate_runtime_environment(settings)


def test_preflight_rejects_invalid_disk_watermark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "local")
    settings = _settings(tmp_path, disk_high_watermark_percent=100)

    with pytest.raises(RuntimeError, match="DISK_HIGH_WATERMARK"):
        validate_runtime_environment(settings)


# ── languages the isolation level cannot run ──


def _toolchains(*names: str):
    from agent_sandbox_runtime import build_toolchain

    options = {
        "index_url": "https://pypi.example/simple/",
        "registry": "https://npm.example",
        "proxy": "https://goproxy.example",
        "registry_url": "https://crates.example",
        "maven_repository_url": "https://maven.example",
    }
    return tuple(build_toolchain(name, **options) for name in names)


def test_basic_isolation_warns_about_the_languages_it_cannot_run() -> None:
    """`basic` mounts no procfs, and the JDK and Rust find their own libraries
    through /proc/self/exe. Without this the operator meets
    "libjli.so: cannot open shared object file" and reasonably concludes the
    image is broken."""
    warnings = toolchain_isolation_warnings(
        {"selected_level": "basic", "features": []},
        _toolchains("python", "node", "go", "rust", "java"),
    )

    assert len(warnings) == 1
    assert "rust, java" in warnings[0]
    # And it says what to do about it.
    assert "standard" in warnings[0]


def test_go_is_not_warned_about_because_goroot_removes_the_need() -> None:
    warnings = toolchain_isolation_warnings(
        {"selected_level": "basic", "features": []}, _toolchains("python", "node", "go")
    )

    assert warnings == []


def test_a_private_procfs_silences_the_warning() -> None:
    warnings = toolchain_isolation_warnings(
        {"selected_level": "standard", "features": ["pid_namespace", "private_procfs"]},
        _toolchains("go", "rust", "java"),
    )

    assert warnings == []


def test_an_unknown_level_is_still_reported_rather_than_assumed_fine() -> None:
    """The probe always names a level, but a report missing one must not read as
    permission."""
    warnings = toolchain_isolation_warnings({}, _toolchains("java"))

    assert len(warnings) == 1
    assert "unknown" in warnings[0]


# ── the documented startup rules ──
#
# docs/CONFIGURATION.md enumerates what a malformed value does ("refused at
# startup, not at the first request that needs it") and lists the checks. Four
# of them have their own tests above -- the heartbeat margin, the disk
# watermark, the non-local advertise host, and the loopback host. The rest are
# driven here, one row per documented rule, because a rule the documentation
# names and nothing tests is a rule that can be deleted without anything
# noticing. The message matters as much as the refusal: it is what tells an
# operator which variable to fix.


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"port": 0}, "HTTP port out of range: 0"),
        ({"port": 70000}, "HTTP port out of range: 70000"),
        ({"uid_start": 0}, "invalid sandbox UID range"),
        ({"uid_start": 30000, "uid_end": 20000}, "invalid sandbox UID range"),
        ({"worker_capacity": 0}, "SANDBOX_WORKER_CAPACITY must be greater than 0"),
        ({"idle_ttl_seconds": 0}, "sandbox idle TTL and maintenance interval"),
        ({"maintenance_interval_seconds": 0}, "sandbox idle TTL and maintenance interval"),
        ({"tmpfs_bytes": 0}, "SANDBOX_TMPFS_BYTES must be greater than 0"),
        ({"max_output_bytes": 0}, "SANDBOX_MAX_OUTPUT_BYTES must be greater than 0"),
        ({"max_file_api_bytes": 0}, "SANDBOX_MAX_FILE_API_BYTES must be greater than 0"),
        ({"default_timeout_seconds": 0}, "SANDBOX_DEFAULT_TIMEOUT_SECONDS must be greater than 0"),
    ],
)
def test_a_documented_rule_refuses_startup_and_names_the_variable(
    tmp_path: Path, overrides: dict[str, object], expected: str
) -> None:
    settings = _settings(tmp_path, **overrides)

    with pytest.raises(RuntimeError, match=re.escape(expected)):
        validate_runtime_environment(settings)


def test_a_missing_binary_is_refused_by_name(tmp_path: Path) -> None:
    settings = _settings(tmp_path, bubblewrap_path=tmp_path / "bin" / "absent")

    with pytest.raises(RuntimeError, match="bubblewrap is missing or not executable"):
        validate_runtime_environment(settings)


def test_a_symlinked_workspace_root_is_refused(tmp_path: Path) -> None:
    """A symlinked root is how one deployment writes into another's sandboxes."""

    settings = _settings(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    settings.workspace_root.parent.mkdir(parents=True, exist_ok=True)
    settings.workspace_root.symlink_to(real, target_is_directory=True)

    with pytest.raises(RuntimeError, match="the workspace root may not be a symlink"):
        validate_runtime_environment(settings)


def test_an_unwritable_workspace_root_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    settings.workspace_root.mkdir()
    reachable = os.access

    def deny_the_root(path: object, mode: int) -> bool:
        if Path(str(path)) == settings.workspace_root:
            return False
        return reachable(path, mode)

    monkeypatch.setattr(os, "access", deny_the_root)

    with pytest.raises(RuntimeError, match="the workspace root is not writable"):
        validate_runtime_environment(settings)


# ── the token ──
#
# docs/CONFIGURATION.md makes three claims about an empty SANDBOX_INTERNAL_TOKEN:
# it is a startup error in `prod`, the service generates one elsewhere, and a
# generated token is never written down -- which is what makes the second claim
# safe rather than merely convenient.


def test_an_empty_token_outside_production_is_generated_and_never_written_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "staging")
    settings = _settings(tmp_path, internal_token="")
    settings.workspace_root.mkdir()

    warnings = validate_runtime_environment(settings)

    assert settings.internal_token, "the service generated nothing to authenticate with"
    generated = [line for line in warnings if "generated a one-off token" in line]
    assert generated, warnings
    assert settings.internal_token not in generated[0], "the warning wrote the token down"


def test_an_empty_token_in_production_stops_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A production worker with no token must not come up reachable but unauthenticated."""

    monkeypatch.setenv("SANDBOX_ENVIRONMENT", "prod")
    settings = _settings(tmp_path, internal_token="")
    settings.workspace_root.mkdir()

    with pytest.raises(RuntimeError, match="SANDBOX_INTERNAL_TOKEN is not configured"):
        validate_runtime_environment(settings)


@pytest.mark.real_host_probe
@pytest.mark.parametrize(("content", "warns"), [("1\n", True), ("0\n", False), (None, False)])
def test_the_apparmor_userns_restriction_is_reported_only_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, content: str | None, warns: bool
) -> None:
    sysctl = tmp_path / "apparmor_restrict_unprivileged_userns"
    if content is not None:
        sysctl.write_text(content, encoding="utf-8")
    monkeypatch.setattr(preflight, "_APPARMOR_USERNS_SYSCTL", sysctl)
    monkeypatch.setattr(preflight, "_has_bounding_capability", lambda _capability: True)
    monkeypatch.setattr(preflight.sys, "platform", "linux")

    found = check_user_namespace_support()

    assert bool(found) is warns
    if warns:
        assert "apparmor_restrict_unprivileged_userns=1" in found[0]


@pytest.mark.real_host_probe
def test_a_root_caller_without_cap_setfcap_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight.sys, "platform", "linux")
    monkeypatch.setattr(preflight.os, "geteuid", lambda: 0)
    monkeypatch.setattr(preflight, "_has_bounding_capability", lambda _capability: False)

    assert any("CAP_SETFCAP" in warning for warning in check_user_namespace_support())
