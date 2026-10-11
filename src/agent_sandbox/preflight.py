"""Fail-fast staging deployment checks that do not contact external services."""

from __future__ import annotations

import ipaddress
import os
import secrets
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from agent_sandbox_runtime import Toolchain, toolchains_unavailable_at

from .config import Settings, get_environment

# CAP_SETFCAP, from <linux/capability.h>.
_CAP_SETFCAP = 31


def toolchain_isolation_warnings(
    capabilities: Mapping[str, object], toolchains: Iterable[Toolchain]
) -> list[str]:
    """Report measured tool failures; fall back to legacy procfs hints.

    Isolation and tool launches are probed separately. In particular, adapted
    JDK/Rust launchers can work at basic while legacy launchers need procfs.
    """

    features = capabilities.get("features")
    measured = capabilities.get("toolchains")
    if isinstance(measured, Mapping) and isinstance(measured.get("checks"), Mapping):
        checks = measured["checks"]
        warnings = []
        configured = {toolchain.name for toolchain in toolchains}
        for name, check in checks.items():
            if name in configured and isinstance(check, Mapping):
                status = check.get("status")
                if status in {"missing", "failed", "timed_out"}:
                    warnings.append(
                        f"toolchain {name}: sandbox launch probe {status} at isolation "
                        f"level {capabilities.get('selected_level', 'unknown')!r}; "
                        f"{check.get('detail', '')}. See docs/TOOLCHAINS.md."
                    )
        return warnings
    needing = toolchains_unavailable_at(
        features if isinstance(features, (list, tuple)) else (), toolchains
    )
    if not needing:
        return []
    level = capabilities.get("selected_level", "unknown")
    return [
        f"toolchains {', '.join(needing)} need /proc/self/exe to locate their own "
        f"libraries, and isolation level {level!r} provides no procfs; their compilers "
        "may fail to start with the default image. Use the measured toolchain checks "
        "where available. See docs/TOOLCHAINS.md for standard isolation prerequisites."
    ]


def validate_runtime_environment(settings: Settings) -> list[str]:
    """Validate static configuration and return non-blocking warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    if not settings.internal_token.strip():
        env = get_environment()
        if env == "prod":
            errors.append("SANDBOX_INTERNAL_TOKEN is not configured")
        else:
            # Outside production, generate a random token so preflight passes. An
            # internal caller still has to obtain it to authenticate; production
            # always requires the token to be set explicitly.
            generated = secrets.token_urlsafe(32)
            object.__setattr__(settings, "internal_token", generated)
            warnings.append(
                f"SANDBOX_INTERNAL_TOKEN is unset; generated a one-off token for the {env} environment"
            )
    if not 1 <= settings.port <= 65535:
        errors.append(f"HTTP port out of range: {settings.port}")
    if settings.uid_start < 1 or settings.uid_end < settings.uid_start:
        errors.append(f"invalid sandbox UID range: {settings.uid_start}-{settings.uid_end}")
    if settings.heartbeat_ttl_seconds <= settings.heartbeat_interval_seconds * 2:
        errors.append("heartbeat TTL must exceed twice the heartbeat interval")
    if settings.worker_capacity < 1:
        errors.append("SANDBOX_WORKER_CAPACITY must be greater than 0")
    if settings.idle_ttl_seconds < 1 or settings.maintenance_interval_seconds < 1:
        errors.append("sandbox idle TTL and maintenance interval must be greater than 0")
    if not 1 <= settings.disk_high_watermark_percent <= 99:
        errors.append("SANDBOX_DISK_HIGH_WATERMARK_PERCENT must be between 1 and 99")
    if settings.min_free_bytes < 0:
        errors.append("SANDBOX_MIN_FREE_BYTES must not be negative")
    for label, value in (
        ("SANDBOX_TMPFS_BYTES", settings.tmpfs_bytes),
        ("SANDBOX_MAX_OUTPUT_BYTES", settings.max_output_bytes),
        ("SANDBOX_MAX_FILE_API_BYTES", settings.max_file_api_bytes),
        ("SANDBOX_DEFAULT_TIMEOUT_SECONDS", settings.default_timeout_seconds),
    ):
        if value < 1:
            errors.append(f"{label} must be greater than 0")

    for label, path in (
        ("bubblewrap", settings.bubblewrap_path),
        ("setpriv", settings.setpriv_path),
        ("prlimit", settings.prlimit_path),
        ("bash", settings.bash_path),
    ):
        if not path.is_file() or not os.access(path, os.X_OK):
            errors.append(f"{label} is missing or not executable: {path}")

    workspace_root = settings.workspace_root
    if workspace_root.exists() and workspace_root.is_symlink():
        errors.append(f"the workspace root may not be a symlink: {workspace_root}")
    if workspace_root.exists() and not os.access(workspace_root, os.W_OK | os.X_OK):
        errors.append(f"the workspace root is not writable: {workspace_root}")

    warnings.extend(check_user_namespace_support())

    if get_environment() != "local":
        host = settings.advertise_host.strip()
        if not host:
            errors.append(
                "a non-local environment must set SANDBOX_ADVERTISE_HOST to an address agents can reach"
            )
        elif _is_loopback(host):
            errors.append(f"a non-local worker endpoint may not be a loopback address: {host}")
        if (
            settings.object_store_backend == "s3"
            and settings.blobstore_endpoint
            and settings.blobstore_bucket
        ):
            if not (
                os.getenv("BLOBSTORE_ACCESS_KEY", "") and os.getenv("BLOBSTORE_SECRET_KEY", "")
            ):
                warnings.append(
                    "BlobStore has an endpoint and bucket but no explicit credentials; "
                    "falling back to the standard boto3 credential chain"
                )

    if errors:
        raise RuntimeError("runtime preflight failed: " + "; ".join(errors))
    return warnings


_APPARMOR_USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def check_user_namespace_support() -> list[str]:
    """Report host settings that break Bubblewrap in ways that are hard to read.

    Both conditions below surface as a bare `Operation not permitted` from
    every sandboxed command, with nothing naming the cause. They are warnings
    rather than errors because isolation negotiation still probes the host for
    the truth — this only makes a probe failure explicable.

    The conditions are the ones Anthropic's sandbox-runtime documents:

    * Ubuntu 24.04+ sets `kernel.apparmor_restrict_unprivileged_userns=1`,
      which lets `unshare(CLONE_NEWUSER)` succeed but strips the capabilities
      the resulting namespace needs.
    * A caller running as root needs `CAP_SETFCAP` in its bounding set: since
      Linux 5.12 a namespace may map uid 0 only when its creator held that
      capability. The bounding set is what counts, because Bubblewrap is
      reached through `execve` and the kernel recomputes a root caller's
      permitted set from it.
    """

    if sys.platform != "linux":
        return []

    warnings: list[str] = []
    try:
        if _APPARMOR_USERNS_SYSCTL.read_text(encoding="utf-8").strip() == "1":
            warnings.append(
                "kernel.apparmor_restrict_unprivileged_userns=1 strips capabilities from new "
                "user namespaces; Bubblewrap probes may fail with 'Operation not permitted'"
            )
    except OSError:
        pass

    if os.geteuid() == 0 and not _has_bounding_capability(_CAP_SETFCAP):
        warnings.append(
            "running as root without CAP_SETFCAP in the bounding set; Bubblewrap cannot map "
            "uid 0 in a new user namespace and every sandboxed command will fail"
        )
    return warnings


def _has_bounding_capability(capability: int) -> bool:
    """Read one capability out of `CapBnd` in `/proc/self/status`."""

    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8")
    except OSError:
        return True  # Unreadable: do not invent a warning.
    for line in status.splitlines():
        if line.startswith("CapBnd:"):
            try:
                mask = int(line.split(":", 1)[1].strip(), 16)
            except ValueError:
                return True
            return bool(mask & (1 << capability))
    return True


def write_startup_marker(path: Path, status: str, detail: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f"agent-sandbox,status={status},{detail}\n")


def _is_loopback(host: str) -> bool:
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False
