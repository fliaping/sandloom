from __future__ import annotations

from typing import Any

import pytest
from agent_sandbox_runtime import build_toolchain

from agent_sandbox.preflight import toolchain_isolation_warnings
from agent_sandbox.schemas import ExecRequest, ExecResponse
from agent_sandbox.toolchain_probe import probe_toolchains


@pytest.mark.parametrize(
    ("response", "status"),
    [
        ({"exit_code": 0, "status": "SUCCEEDED", "stdout": "rustc 1.90"}, "available"),
        (
            {"exit_code": 127, "status": "FAILED", "stdout": "sandbox-tool-missing:rustc\n"},
            "missing",
        ),
        ({"exit_code": 127, "status": "FAILED", "stderr": "missing shared library"}, "failed"),
        ({"exit_code": -9, "status": "TIMED_OUT"}, "timed_out"),
    ],
)
async def test_launch_results_distinguish_installation_and_execution(
    response: dict[str, Any], status: str
) -> None:
    async def execute(request: ExecRequest) -> ExecResponse:
        assert request.timeout_seconds == 10
        assert "rustc --version" in request.argv[-1]
        assert "cargo --version" in request.argv[-1]
        return ExecResponse(exec_id=request.exec_id, **response)

    checks = await probe_toolchains(["rust"], execute)
    assert checks["rust"]["status"] == status


async def test_node_checks_optional_typescript_without_installing_it() -> None:
    seen = []

    async def execute(request: ExecRequest) -> ExecResponse:
        seen.append(request.argv[-1])
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED", exit_code=0)

    checks = await probe_toolchains(["node"], execute)
    assert set(checks) == {"node", "typescript"}
    assert "tsc --version" in seen[1]
    assert all("npx" not in script and "npm install" not in script for script in seen)


def test_measured_basic_success_overrides_static_procfs_hint() -> None:
    warnings = toolchain_isolation_warnings(
        {
            "selected_level": "basic",
            "features": [],
            "toolchains": {"checks": {"java": {"status": "available"}}},
        },
        [build_toolchain("java")],
    )
    assert warnings == []


def test_missing_compiler_is_reported_even_at_strict() -> None:
    warnings = toolchain_isolation_warnings(
        {
            "selected_level": "strict",
            "features": ["private_procfs"],
            "toolchains": {
                "checks": {
                    "java": {"status": "missing", "detail": "sandbox-tool-missing:javac"},
                    "typescript": {"status": "missing"},
                }
            },
        },
        [build_toolchain("java")],
    )
    assert len(warnings) == 1
    assert "javac" in warnings[0]
    assert "typescript" not in warnings[0]


async def test_failure_diagnostics_are_bounded() -> None:
    async def execute(request: ExecRequest) -> ExecResponse:
        return ExecResponse(
            exec_id=request.exec_id, status="FAILED", exit_code=1, stderr="x" * 100_000
        )

    checks = await probe_toolchains(["java"], execute)
    assert len(checks["java"]["detail"]) == 1000
