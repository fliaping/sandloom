"""Local, machine-readable environment diagnosis without external services."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .preflight import check_user_namespace_support
from .runtime import SandboxRuntime


async def diagnose(settings: Settings) -> dict[str, object]:
    """Run real probes in disposable directories, not production workspaces.

    This is an execution diagnostic, not a DB/S3 readiness check or security
    certification. No external services, enterprise plugins or HTTP server
    are loaded. An inventory fallback never changes the requested policy.
    """
    report: dict[str, object] = {
        "schema_version": 1,
        "product": "Sandloom",
        "status": "blocked",
        "can_start": False,
        "environment": {
            "system": platform.system(),
            "kernel": platform.release(),
            "architecture": platform.machine(),
            "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
        },
        "requested_policy": {
            "level": settings.isolation_level,
            "network_mode": settings.network_mode,
            "required_features": settings.isolation_required_features,
            "optional_features": settings.isolation_optional_features,
        },
        "warnings": check_user_namespace_support(),
        "scope": "local execution only; no database, Redis, S3 or private plugin checks",
        "errors": [],
    }
    if sys.platform != "linux":
        report["errors"] = ["Bubblewrap requires Linux. Run doctor inside the worker image."]
        return report
    if settings.execution_backend != "bubblewrap":
        report["errors"] = [
            "The built-in doctor does not execute private or third-party backends. "
            "Use their own diagnostic command and verify the running /healthz report."
        ]
        return report
    if os.geteuid() != 0:
        report["errors"] = [
            "The UID-separated service requires root inside its trusted Linux worker. "
            "This does not require privileged Docker or host root."
        ]
        return report
    with tempfile.TemporaryDirectory(prefix="sandloom-doctor-") as directory:
        root = Path(directory)
        # Sandbox UIDs must be able to traverse the disposable parent.
        await asyncio.to_thread(root.chmod, 0o711)
        diagnostic = settings.model_copy(
            update={
                "local_root": root / "sandboxes",
                "shared_root": None,
                "template_root": root / "templates",
                "object_store_backend": "disabled",
                "telemetry_sink": None,
                "credential_broker": "disabled",
            }
        )
        try:
            report["capabilities"] = await SandboxRuntime(diagnostic).probe()
        except (RuntimeError, ValueError, OSError) as exc:
            report["errors"] = [str(exc)[:6000]]
            # Keep the failed policy visible while discovering alternatives.
            inventory = diagnostic.model_copy(
                update={
                    "isolation_level": "auto",
                    "isolation_required_features": [],
                    "isolation_optional_features": sorted(
                        set(settings.isolation_required_features)
                        | set(settings.isolation_optional_features)
                    ),
                }
            )
            try:
                report["inventory"] = await SandboxRuntime(inventory).probe()
            except (RuntimeError, ValueError, OSError) as inventory_exc:
                report["inventory_error"] = str(inventory_exc)[:6000]
        else:
            report.update(status="ready", can_start=True)
    report["operator_guidance"] = [
        "Use required features for hard requirements; optional features may be skipped.",
        "Review probe failures against outer seccomp, AppArmor and kernel policies.",
        "Do not automatically grant privileged mode, SYS_ADMIN or mount the Docker socket.",
        "Re-run doctor and verify /healthz after an operator-approved configuration change.",
    ]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose Sandloom's local execution boundary")
    parser.add_argument("--json", action="store_true", help="machine-readable report for Agents")
    args = parser.parse_args()
    try:
        report = asyncio.run(diagnose(Settings()))
    except ValidationError as exc:
        # Pydantic's default string includes input values (potential secrets).
        report = {
            "schema_version": 1,
            "status": "blocked",
            "can_start": False,
            "errors": [
                {"field": list(item["loc"]), "type": item["type"]}
                for item in exc.errors(include_input=False, include_context=False)
            ],
        }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Sandloom doctor: {report['status']}")
        print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["can_start"] else 2)


if __name__ == "__main__":
    main()
