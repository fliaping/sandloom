"""Stateless Streamable HTTP MCP facade for Sandbox operations."""

from __future__ import annotations

from importlib.metadata import version
from secrets import compare_digest
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types.version import LATEST_PROTOCOL_VERSION
from mcp_types import ToolAnnotations
from pydantic import BaseModel
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings
from .models import Route
from .schemas import (
    ExecRequest,
    ExecResponse,
    ResolveRequest,
    ResumeResponse,
    SandboxAuditResponse,
    SuspendResponse,
)
from .service import SandboxService, route_audit
from .storage import MetadataStore

MCP_PROXY_PATH = "/api/v1/sandboxes/mcp/streamable-http"


class ProfileInfo(BaseModel):
    profile_id: str
    profile_hash: str
    mcp_sdk_version: str
    transport: str = "streamable-http"
    stateless: bool = True
    worker_id: str
    healthy: bool
    running_sessions: int
    running_execs: int
    capabilities: dict[str, object]
    orphan_reaper: dict[str, object]


class SandboxRouteInfo(BaseModel):
    sandbox_id: str
    generation: int
    status: str
    storage_mode: str
    profile_id: str
    profile_hash: str
    worker_id: str | None


class SandboxOperationResult(BaseModel):
    sandbox_id: str
    generation: int
    status: str


class FileContent(BaseModel):
    path: str
    content_base64: str


class CancelResult(BaseModel):
    cancelled: bool


class ReleaseResult(BaseModel):
    sandbox_id: str
    status: str = "RELEASED"


READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
EXECUTE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
RELEASE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)


class InternalBearerAuth:
    """Protect the mounted MCP ASGI app with the existing internal token."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.enabled = bool(token)
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        actual = headers.get(b"authorization", b"")
        if not self.enabled or not compare_digest(actual, self.expected):
            body = b'{"error":"internal service authentication failed"}'
            start: Message = {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
            await send(start)
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def build_mcp_server(
    settings: Settings,
    database: MetadataStore,
    service: SandboxService,
) -> MCPServer[None]:
    """Build the official SDK's stateless Streamable HTTP MCP server."""
    mcp: MCPServer[None] = MCPServer(
        "agent-sandbox",
        instructions=(
            "Operate Sandboxes through the unified Control Plane. Always keep the generation "
            "returned by sandbox_resolve and call sandbox_release in a finally step. "
            "While waiting on something slow, call sandbox_suspend to give the slot back "
            "and sandbox_resume (or sandbox_resolve) to continue with the same workspace."
        ),
    )

    async def route_for_generation(sandbox_id: str, generation: int) -> Route:
        route = await database.find_route(sandbox_id)
        if route is None or route.generation != generation:
            raise RuntimeError("STALE_SANDBOX_ROUTE")
        return route

    @mcp.tool(
        name="sandbox_profile",
        title="Sandbox runtime profile",
        description="Return the currently deployed runtime profile hash and worker health.",
        annotations=READ_ONLY,
    )
    async def sandbox_profile() -> ProfileInfo:
        return ProfileInfo(
            profile_id=settings.profile_id,
            profile_hash=settings.profile_hash,
            mcp_sdk_version=f"{version('mcp')} (protocol {LATEST_PROTOCOL_VERSION})",
            worker_id=service.worker_id,
            healthy=service.healthy,
            running_sessions=len(service.runtime.sandboxes),
            running_execs=len(service.runtime.processes),
            capabilities=service.capabilities,
            orphan_reaper=service.reaper_status,
        )

    @mcp.tool(
        name="sandbox_resolve",
        description="Resolve or idempotently reuse a Sandbox route.",
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_resolve(
        sandbox_id: str,
        workspace_scope_id: str,
        profile: str = "coding-default",
    ) -> SandboxRouteInfo:
        await service.resolve(
            ResolveRequest(
                sandbox_id=sandbox_id,
                workspace_scope_id=workspace_scope_id,
                profile=profile,
            )
        )
        route = await database.find_route(sandbox_id)
        if route is None:  # pragma: no cover - service.resolve guarantees this
            raise RuntimeError("SANDBOX_NOT_FOUND")
        return _route_info(route)

    @mcp.tool(
        name="sandbox_create",
        description="Create the resolved Sandbox generation on its assigned Worker.",
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_create(sandbox_id: str, generation: int) -> SandboxOperationResult:
        route = await route_for_generation(sandbox_id, generation)
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}",
            json_body={
                "generation": route.generation,
                "sandbox_uid": route.sandbox_uid,
                "profile": route.profile_id,
                "worker_epoch": route.worker_epoch,
            },
        )
        payload = _worker_payload(response)
        return SandboxOperationResult(
            sandbox_id=sandbox_id,
            generation=generation,
            status=str(payload.get("status", "READY")),
        )

    @mcp.tool(
        name="sandbox_status",
        description="Return the authoritative current route and runtime profile hash.",
        annotations=READ_ONLY,
    )
    async def sandbox_status(sandbox_id: str) -> SandboxRouteInfo:
        route = await database.find_route(sandbox_id)
        if route is None:
            raise RuntimeError("SANDBOX_NOT_FOUND")
        return _route_info(route)

    @mcp.tool(
        name="sandbox_audit_get",
        description=(
            "Return lifecycle audit timestamps, actors, release reason, and usage duration."
        ),
        annotations=READ_ONLY,
    )
    async def sandbox_audit_get(sandbox_id: str) -> SandboxAuditResponse:
        route = await database.find_route(sandbox_id)
        if route is None:
            raise RuntimeError("SANDBOX_NOT_FOUND")
        return route_audit(route)

    @mcp.tool(
        name="sandbox_exec",
        description="Execute an idempotent command identified by exec_id inside a Sandbox.",
        annotations=EXECUTE,
    )
    async def sandbox_exec(
        sandbox_id: str,
        generation: int,
        exec_id: str,
        argv: list[str],
        cwd: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        background: bool = False,
        exec_scope: str | None = None,
    ) -> ExecResponse:
        request = ExecRequest(
            exec_id=exec_id,
            generation=generation,
            argv=argv,
            cwd=cwd,
            env=env or {},
            timeout_seconds=timeout_seconds,
            background=background,
            exec_scope=exec_scope,
        )
        timeout = (
            30.0
            if background
            else float(
                (timeout_seconds or settings.default_timeout_seconds)
                + settings.terminate_grace_seconds
                + 15
            )
        )
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec",
            json_body=request.model_dump(),
            timeout_seconds=timeout,
        )
        return ExecResponse.model_validate(_worker_payload(response))

    @mcp.tool(
        name="sandbox_exec_status",
        description="Read the persisted result for a Sandbox exec_id.",
        annotations=READ_ONLY,
    )
    async def sandbox_exec_status(
        sandbox_id: str,
        generation: int,
        exec_id: str,
    ) -> ExecResponse:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="GET",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}",
        )
        return ExecResponse.model_validate(_worker_payload(response))

    @mcp.tool(
        name="sandbox_cancel",
        description="Cancel a running Sandbox command if it is still active.",
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_cancel(
        sandbox_id: str,
        generation: int,
        exec_id: str,
    ) -> CancelResult:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}/cancel",
        )
        return CancelResult.model_validate(_worker_payload(response))

    @mcp.tool(
        name="sandbox_write_file",
        description="Write base64-encoded content below /workspace.",
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_write_file(
        sandbox_id: str,
        generation: int,
        path: str,
        content_base64: str,
    ) -> SandboxOperationResult:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="PUT",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files",
            json_body={
                "generation": generation,
                "path": path,
                "content_base64": content_base64,
            },
            timeout_seconds=60.0,
        )
        _worker_payload(response)
        return SandboxOperationResult(
            sandbox_id=sandbox_id,
            generation=generation,
            status="WRITTEN",
        )

    @mcp.tool(
        name="sandbox_read_file",
        description="Read a file below /workspace as base64-encoded content.",
        annotations=READ_ONLY,
    )
    async def sandbox_read_file(
        sandbox_id: str,
        generation: int,
        path: str,
    ) -> FileContent:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="GET",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files",
            params={"path": path, "generation": generation},
            timeout_seconds=60.0,
        )
        return FileContent.model_validate(_worker_payload(response))

    @mcp.tool(
        name="sandbox_suspend",
        description=(
            "Release the Sandbox capacity slot and keep its workspace until resumed. "
            "Refused while a command runs; repeating it is a no-op. Running processes "
            "are not preserved."
        ),
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_suspend(sandbox_id: str, generation: int) -> SuspendResponse:
        return await service.suspend(sandbox_id, generation)

    @mcp.tool(
        name="sandbox_resume",
        description=(
            "Wake a suspended Sandbox with its workspace and return the route to use; "
            "a no-op when it is awake. Use the returned generation from then on."
        ),
        annotations=IDEMPOTENT_WRITE,
    )
    async def sandbox_resume(sandbox_id: str) -> ResumeResponse:
        return await service.resume(sandbox_id)

    @mcp.tool(
        name="sandbox_release",
        description="Release the current Sandbox route and destroy its runtime.",
        annotations=RELEASE,
    )
    async def sandbox_release(sandbox_id: str) -> ReleaseResult:
        route = await database.find_route(sandbox_id)
        if route is not None:
            await service.release(route)
        return ReleaseResult(sandbox_id=sandbox_id)

    return mcp


def authenticated_mcp_app(
    mcp: MCPServer[Any],
    token: str,
    settings: Settings,
) -> ASGIApp:
    """Return the bearer-protected stateless MCP ASGI application."""
    allowed_hosts = list(
        dict.fromkeys(
            [
                *settings.mcp_allowed_hosts,
                settings.advertise_host,
                f"{settings.advertise_host}:{settings.port}",
            ]
        )
    )
    app = mcp.streamable_http_app(
        streamable_http_path=MCP_PROXY_PATH,
        json_response=True,
        stateless_http=True,
        max_request_body_size=settings.max_file_api_bytes + 1024 * 1024,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[],
        ),
        host=settings.advertise_host,
    )
    return InternalBearerAuth(app, token)


def _route_info(route: Route) -> SandboxRouteInfo:
    return SandboxRouteInfo(
        sandbox_id=route.sandbox_id,
        generation=route.generation,
        status=route.status,
        storage_mode=route.storage_mode,
        profile_id=route.profile_id,
        profile_hash=route.profile_hash,
        worker_id=route.worker_id,
    )


def _worker_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = response.text.strip() or f"SANDBOX_WORKER_HTTP_{response.status_code}"
        raise RuntimeError(code) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("SANDBOX_WORKER_INVALID_RESPONSE")
    return payload


__all__ = [
    "MCP_PROXY_PATH",
    "authenticated_mcp_app",
    "build_mcp_server",
]
