"""Measured tool availability, independently of namespace isolation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from .schemas import ExecRequest, ExecResponse

# These checks launch installed tools without downloading dependencies or
# compiling projects. The deployment verifier separately tests compilation.
_LAUNCH_CHECKS: dict[str, tuple[tuple[str, ...], str]] = {
    "python": (("python3",), "python3 -c 'print(6 * 7)'"),
    "node": (("node",), "node -e 'console.log(6 * 7)'"),
    "go": (("go",), "go version"),
    "rust": (("rustc", "cargo"), "rustc --version && cargo --version"),
    "java": (("javac", "java"), "javac -version && java -version"),
    "typescript": (("tsc", "node"), "tsc --version && node -e 'console.log(6 * 7)'"),
}
_MISSING_MARKER = "sandbox-tool-missing:"


async def probe_toolchains(
    configured: Iterable[str],
    execute: Callable[[ExecRequest], Awaitable[ExecResponse]],
) -> dict[str, dict[str, Any]]:
    """Launch tools through the selected sandbox, not in the manager.

    Failure is diagnostic and never changes the selected isolation level.
    TypeScript is an optional Node package, so it is checked when Node is
    enabled without becoming a required toolchain.
    """
    names = list(configured)
    if "node" in names:
        names.append("typescript")
    checks: dict[str, dict[str, Any]] = {}
    for name in names:
        definition = _LAUNCH_CHECKS.get(name)
        if definition is None:
            checks[name] = {"status": "not_checked", "check": "launch"}
            continue
        binaries, launch = definition
        presence = "\n".join(
            f"command -v {binary} >/dev/null 2>&1 || "
            f"{{ echo '{_MISSING_MARKER}{binary}'; exit 127; }}"
            for binary in binaries
        )
        request = ExecRequest(
            exec_id=f"toolchain-{name}",
            generation=1,
            argv=["/bin/sh", "-c", f"set -e\n{presence}\n{launch}"],
            timeout_seconds=10,
        )
        try:
            result = await execute(request)
        except (OSError, RuntimeError) as exc:
            checks[name] = {
                "status": "failed",
                "check": "launch",
                "detail": f"{type(exc).__name__}: {exc}"[:1000],
            }
            continue
        output = (result.stdout + result.stderr).strip()
        if result.status == "TIMED_OUT":
            status = "timed_out"
        elif result.exit_code == 127 and output.startswith(_MISSING_MARKER):
            status = "missing"
        elif result.exit_code == 0:
            status = "available"
        else:
            status = "failed"
        checks[name] = {
            "status": status,
            "check": "launch",
            "exit_code": result.exit_code,
            "detail": output[:1000],
        }
    return checks
