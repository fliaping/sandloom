"""FastAPI entry point: unified Control API + local Worker API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from secrets import compare_digest
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .admin import (
    MAX_PAGE_SIZE,
    clamp_page,
    exec_detail,
    exec_summary,
    sandbox_summary,
    worker_summaries,
)
from .backends import DirectoryOperations, as_directory_operations
from .config import Settings, get_settings
from .console import console_html
from .mcp_server import authenticated_mcp_app, build_mcp_server
from .observability import RequestLoggingMiddleware, error_code, event, logging_status
from .registry import create_worker_registry
from .schemas import (
    AdminExecDetail,
    AdminExecListResponse,
    AdminOverviewResponse,
    AdminSandboxListResponse,
    ConnectSandboxRequest,
    CreateSandboxRequest,
    DeletePathRequest,
    DirectoryEntry,
    DirectoryListResponse,
    ExecRequest,
    ExecResponse,
    FileReadResponse,
    FileWriteRequest,
    MakeDirectoryRequest,
    MovePathRequest,
    ResolveRequest,
    RouteResponse,
    SandboxAuditResponse,
    TemplateAttachRequest,
    TemplateBuildRequest,
    TemplateListResponse,
    TemplateResponse,
)
from .service import SandboxService, _exec_from_row, route_audit
from .storage import FleetQueries, as_fleet_queries, create_metadata_store
from .templates import TemplateError
from .threading import complete_in_thread


async def require_internal_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    config: Settings = request.app.state.sandbox_settings
    if not config.internal_token:
        raise HTTPException(status_code=503, detail="SANDBOX_INTERNAL_TOKEN is not configured")
    expected = f"Bearer {config.internal_token}"
    if authorization is None or not compare_digest(authorization.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="internal service authentication failed")


InternalAuth = Annotated[None, Depends(require_internal_auth)]

_REQUEST_FIELDS = frozenset().union(
    *(
        model.model_fields
        for model in (
            ConnectSandboxRequest,
            CreateSandboxRequest,
            ExecRequest,
            FileWriteRequest,
            ResolveRequest,
        )
    )
)


def safe_validation_response(errors: object) -> JSONResponse:
    """Validation input, context, messages and dynamic map keys can contain secrets."""
    safe_errors = []
    if isinstance(errors, list):
        for error in errors[:20]:
            if not isinstance(error, dict):
                continue
            loc = error.get("loc", [])
            field = loc[1] if isinstance(loc, (list, tuple)) and len(loc) > 1 else None
            safe_errors.append(
                {
                    "loc": [
                        "body",
                        field if isinstance(field, str) and field in _REQUEST_FIELDS else "request",
                    ],
                    "type": "invalid_request",
                    "msg": "request field does not satisfy the sandbox contract",
                }
            )
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": "SANDBOX_INVALID_REQUEST",
                "message": "request validation failed",
                "errors": safe_errors,
            },
        },
    )


def relay_worker_response(response: httpx.Response) -> Response:
    """Preserve Worker results, but sanitize validation errors from older workers too."""
    if response.status_code == 422:
        try:
            data = response.json()
        except ValueError:
            data = {}
        detail = data.get("detail", []) if isinstance(data, dict) else []
        errors = detail.get("errors", []) if isinstance(detail, dict) else detail
        return safe_validation_response(errors)
    headers = {}
    if content_type := response.headers.get("content-type"):
        headers["content-type"] = content_type
    return Response(content=response.content, status_code=response.status_code, headers=headers)


# Which HTTP status a rejected request answers with. A status code is a promise
# about who should act next: 4xx says the caller must change something, 5xx says
# the caller may retry. Getting that backwards produces a retry loop that can
# never succeed, and a client cannot tell the difference from the body alone.
#
# This is policy, so it lives at module scope where it can be tested without a
# worker: the exceptions that carry these codes are raised from the template
# manager, the runtime, and the service, and reaching any of them over HTTP
# would otherwise need a live sandbox.
_STATUS_BY_CODE = {
    # The request is wrong and will stay wrong.
    "SANDBOX_TEMPLATE_SOURCE_NOT_A_DIRECTORY": 400,
    # A capability the configured backend does not have. Retrying cannot add it,
    # which is why it is not 503; the other two capability gaps answer 501 too.
    "SANDBOX_TEMPLATES_UNSUPPORTED": 501,
    # Everyone is busy. Retry, but not immediately.
    "SANDBOX_PARALLEL_EXEC_LIMIT": 429,
    # Someone else owns this resource right now, or the caller's fencing token
    # is stale and it needs to resolve again.
    "SANDBOX_BUSY_OR_STALE": 409,
    "STALE_SANDBOX_GENERATION": 409,
    "SANDBOX_SCOPE_MISMATCH": 409,
    "SANDBOX_PROFILE_MISMATCH": 409,
    "SANDBOX_WORKSPACE_LOST": 409,
    "STALE_SANDBOX_ROUTE": 409,
    "SANDBOX_SHARED_WORKSPACE_LOCKED": 409,
    "SANDBOX_EXEC_SCOPE_BUSY": 409,
    "SANDBOX_EXEC_SCOPE_LOCKED": 409,
    "SANDBOX_FILE_PATH_LOCKED": 409,
    # The deployment's object store, not the request: templates cannot be shared
    # until an operator fixes the bucket, and a retry after that is the same call.
    "OBJECT_STORE_UNAVAILABLE": 503,
}
# Retryable worker state: unreachable, disk pressure, no capacity right now.
_DEFAULT_ERROR_STATUS = 503


def reclamation_periods(config: Settings) -> dict[str, float]:
    """The periods this worker reclaims on, as it is actually running them.

    Published by `/healthz` and by the admin overview, from one place: a
    verification that waits for a reaper has to know how long to wait, and an
    operator asking why nothing has been reclaimed needs the numbers rather than
    the defaults of the release they think they are running.
    """

    return {
        "maintenance_interval_seconds": config.maintenance_interval_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "heartbeat_ttl_seconds": config.heartbeat_ttl_seconds,
        "idle_ttl_seconds": config.idle_ttl_seconds,
        "orphan_running_grace_seconds": config.orphan_running_grace_seconds,
        "orphan_release_grace_seconds": config.orphan_release_grace_seconds,
    }


def status_for_error(exc: BaseException) -> int:
    """Map a rejected request onto the status the client should see."""
    if isinstance(exc, TemplateError):
        # A template that is missing, corrupt, or too large is a property of
        # the request, not of this worker's momentary state.
        return 400
    return _STATUS_BY_CODE.get(str(exc), _DEFAULT_ERROR_STATUS)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or get_settings()
    database = create_metadata_store(config)
    fleet = as_fleet_queries(database)
    registry = create_worker_registry(config)
    service = SandboxService(config, database, registry)
    mcp_server = build_mcp_server(config, database, service)
    mcp_asgi = authenticated_mcp_app(mcp_server, config.internal_token, config)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as resources:
            # Register before acquisition so partially initialized resources
            # also close. A failing cleanup must not skip the remaining ones.
            resources.push_async_callback(database.close)
            resources.push_async_callback(registry.close)
            await database.connect()
            resources.push_async_callback(service.stop)
            await service.start()
            async with mcp_server.session_manager.run():
                yield

    app = FastAPI(
        title="Sandloom",
        version="0.2.0",
        description="High-density multi-agent sandboxes with pluggable control-plane adapters.",
        lifespan=lifespan,
    )
    app.state.sandbox_service = service
    app.add_middleware(RequestLoggingMiddleware)
    app.state.sandbox_settings = config
    app.state.mcp_server = mcp_server

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return safe_validation_response(exc.errors())

    @app.exception_handler(RuntimeError)
    async def runtime_error_handler(request: Request, exc: RuntimeError) -> PlainTextResponse:
        event(
            "sandbox_request_rejected",
            level=30,
            error_code=error_code(exc),
            sandbox_id=request.path_params.get("sandbox_id"),
            exec_id=request.path_params.get("exec_id"),
        )
        code = str(exc)
        return PlainTextResponse(code, status_code=status_for_error(exc))

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> PlainTextResponse:
        return PlainTextResponse(str(exc), status_code=400)

    @app.get("/health", response_class=PlainTextResponse)
    async def health() -> PlainTextResponse:
        # Liveness probe: orchestrators only need a 200 response body.
        return PlainTextResponse("ok", status_code=200)

    @app.get("/healthz", tags=["internal"])
    async def healthz() -> dict[str, object]:
        from .config import get_environment_type

        disk = service.runtime.disk_status()
        return {
            "status": "ok" if service.healthy and disk["available"] else "degraded",
            "mode": "agent-sandbox",
            "logging": logging_status(),
            "api_capabilities": ["redacted_validation_v1"],
            "db": database.describe(),
            "env_type": get_environment_type(),
            "worker": {
                "id": service.worker_id,
                "epoch": service.worker_epoch,
                "endpoint": config.worker_endpoint,
                "storage_mode": "shared" if config.shared_root else "local",
                "running_sessions": len(service.runtime.sandboxes),
                "running_execs": len(service.runtime.processes),
                "capabilities": service.capabilities,
                "profile_id": config.profile_id,
                "profile_hash": config.profile_hash,
                "orphan_reaper": service.reaper_status,
                "reclamation": reclamation_periods(config),
            },
            "disk": disk,
        }

    @app.post("/api/v1/sandboxes/resolve", response_model=RouteResponse)
    async def resolve(request: ResolveRequest, _: InternalAuth) -> RouteResponse:
        return await service.resolve(request)

    @app.get("/api/v1/sandboxes/{sandbox_id}")
    async def sandbox_status(sandbox_id: str, _: InternalAuth) -> dict[str, object]:
        route = await database.find_route(sandbox_id)
        if route is None:
            raise HTTPException(status_code=404, detail="SANDBOX_NOT_FOUND")
        return asdict(route)

    @app.get(
        "/api/v1/sandboxes/{sandbox_id}/audit",
        response_model=SandboxAuditResponse,
    )
    async def sandbox_audit(sandbox_id: str, _: InternalAuth) -> SandboxAuditResponse:
        route = await database.find_route(sandbox_id)
        if route is None:
            raise HTTPException(status_code=404, detail="SANDBOX_NOT_FOUND")
        return route_audit(route)

    @app.post("/api/v1/sandboxes/{sandbox_id}")
    async def connect_sandbox(
        sandbox_id: str,
        request: ConnectSandboxRequest,
        _: InternalAuth,
    ) -> Response:
        route = await database.find_route(sandbox_id)
        if route is None or route.generation != request.generation:
            raise RuntimeError("STALE_SANDBOX_ROUTE")
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}",
            json_body={
                "generation": route.generation,
                "sandbox_uid": route.sandbox_uid,
                "profile": route.profile_id,
                "worker_epoch": route.worker_epoch,
            },
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/exec")
    async def proxy_exec(
        sandbox_id: str,
        request: ExecRequest,
        _: InternalAuth,
    ) -> Response:
        timeout = (
            30.0
            if request.background
            else float(
                (request.timeout_seconds or config.default_timeout_seconds)
                + config.terminate_grace_seconds
                + 15
            )
        )
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec",
            json_body=request.model_dump(),
            timeout_seconds=timeout,
        )
        return relay_worker_response(response)

    @app.get("/api/v1/sandboxes/{sandbox_id}/exec/{exec_id}")
    async def proxy_exec_status(
        sandbox_id: str,
        exec_id: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="GET",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}",
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/exec/{exec_id}/cancel")
    async def proxy_cancel_exec(
        sandbox_id: str,
        exec_id: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}/cancel",
        )
        return relay_worker_response(response)

    @app.put("/api/v1/sandboxes/{sandbox_id}/files")
    async def proxy_write_file(
        sandbox_id: str,
        request: FileWriteRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="PUT",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files",
            json_body=request.model_dump(),
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.get("/api/v1/sandboxes/{sandbox_id}/files")
    async def proxy_read_file(
        sandbox_id: str,
        path: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="GET",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files",
            params={"path": path, "generation": generation},
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.get("/api/v1/sandboxes/{sandbox_id}/files/list", response_model=DirectoryListResponse)
    async def proxy_list_directory(
        sandbox_id: str,
        path: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=generation,
            method="GET",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files/list",
            params={"path": path, "generation": generation, "limit": limit, "offset": offset},
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/files/mkdir")
    async def proxy_make_directory(
        sandbox_id: str,
        request: MakeDirectoryRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files/mkdir",
            json_body=request.model_dump(),
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/files/delete")
    async def proxy_delete_path(
        sandbox_id: str,
        request: DeletePathRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files/delete",
            json_body=request.model_dump(),
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/files/move")
    async def proxy_move_path(
        sandbox_id: str,
        request: MovePathRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/files/move",
            json_body=request.model_dump(),
            timeout_seconds=60.0,
        )
        return relay_worker_response(response)

    @app.post("/api/v1/sandboxes/{sandbox_id}/templates")
    async def proxy_build_template(
        sandbox_id: str,
        request: TemplateBuildRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="POST",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/templates",
            json_body=request.model_dump(),
            # Archiving and uploading a multi-gigabyte environment is slow, and
            # a timeout here would strand a half-published template.
            timeout_seconds=900.0,
        )
        return relay_worker_response(response)

    @app.put("/api/v1/sandboxes/{sandbox_id}/templates")
    async def proxy_attach_templates(
        sandbox_id: str,
        request: TemplateAttachRequest,
        _: InternalAuth,
    ) -> Response:
        response = await service.proxy_to_worker(
            sandbox_id=sandbox_id,
            generation=request.generation,
            method="PUT",
            internal_path=f"/internal/v1/sandboxes/{sandbox_id}/templates",
            json_body=request.model_dump(),
            # A cold worker has to download and extract before it can answer.
            timeout_seconds=600.0,
        )
        return relay_worker_response(response)

    @app.get("/api/v1/templates", response_model=TemplateListResponse)
    async def list_templates(_: InternalAuth) -> TemplateListResponse:
        return TemplateListResponse(
            templates=[TemplateResponse(**record.as_dict()) for record in service.list_templates()]
        )

    @app.delete("/api/v1/templates/{name}")
    async def delete_template(name: str, _: InternalAuth) -> dict[str, str]:
        removed = service.remove_template(name)
        return {"status": "REMOVED" if removed else "NOT_FOUND", "name": name}

    @app.delete("/api/v1/sandboxes/{sandbox_id}")
    async def release(sandbox_id: str, _: InternalAuth) -> dict[str, str]:
        route = await database.find_route(sandbox_id)
        if route is None:
            return {"status": "RELEASED"}
        await service.release(route)
        return {"status": "RELEASED"}

    # ── admin console API ──
    #
    # Fleet-wide reads for operators. These need a metadata store that can scan,
    # which the built-in SQL store does and a third-party plugin may not, so
    # each route says so explicitly instead of returning an empty list that
    # would look like an idle fleet.

    def _fleet_or_raise() -> FleetQueries:
        if fleet is None:
            raise HTTPException(
                status_code=501,
                detail="SANDBOX_FLEET_QUERIES_UNSUPPORTED",
            )
        return fleet

    # Directory operations are optional for the same reason: an execution
    # backend from an earlier release implements only single-file reads and
    # writes, and must keep working rather than fail to load.
    directory_ops = as_directory_operations(service.runtime)

    def _directory_or_raise() -> DirectoryOperations:
        if directory_ops is None:
            raise HTTPException(
                status_code=501,
                detail="SANDBOX_DIRECTORY_OPS_UNSUPPORTED",
            )
        return directory_ops

    @app.get("/api/v1/admin/overview", response_model=AdminOverviewResponse)
    async def admin_overview(_: InternalAuth) -> AdminOverviewResponse:
        snapshot = await registry.runtime_snapshot()
        isolation = dict(service.capabilities)
        try:
            template_total = len(service.list_templates())
        except RuntimeError:
            # Templates are optional; an unsupported backend must not take down
            # the whole landing page.
            template_total = 0
        if fleet is None:
            live = [item for item in snapshot.get("workers", []) if isinstance(item, dict)]
            return AdminOverviewResponse(
                sandboxes_by_status={},
                sandbox_total=0,
                workers=[],
                worker_total=len(live),
                live_worker_total=len(live),
                capacity_total=sum(int(item.get("capacity") or 0) for item in live),
                running_sessions_total=sum(int(item.get("running_sessions") or 0) for item in live),
                template_total=template_total,
                templates_shared=service.templates_are_shared,
                isolation=isolation,
                disk=dict(service.runtime.disk_status()),
                reclamation=reclamation_periods(config),
                fleet_queries_available=False,
            )
        by_status = await fleet.count_routes_by_status()
        workers = worker_summaries(
            await fleet.list_workers(),
            snapshot,
            heartbeat_ttl_seconds=config.heartbeat_ttl_seconds,
        )
        return AdminOverviewResponse(
            sandboxes_by_status=by_status,
            sandbox_total=sum(by_status.values()),
            workers=workers,
            worker_total=len(workers),
            live_worker_total=sum(1 for item in workers if item.live),
            # Only live workers contribute capacity: a stale row is not somewhere
            # a new sandbox can actually be placed.
            capacity_total=sum(item.capacity for item in workers if item.live),
            running_sessions_total=sum(item.running_sessions for item in workers if item.live),
            template_total=template_total,
            templates_shared=service.templates_are_shared,
            isolation=isolation,
            disk=dict(service.runtime.disk_status()),
            reclamation=reclamation_periods(config),
            fleet_queries_available=True,
        )

    @app.get("/api/v1/admin/sandboxes", response_model=AdminSandboxListResponse)
    async def admin_list_sandboxes(
        _: InternalAuth,
        status: str | None = Query(default=None, max_length=32),
        workspace_scope_id: str | None = Query(default=None, max_length=191),
        worker_id: str | None = Query(default=None, max_length=128),
        search: str | None = Query(default=None, max_length=128),
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> AdminSandboxListResponse:
        queries = _fleet_or_raise()
        limit, offset = clamp_page(limit, offset)
        routes, total = await queries.list_routes(
            status=status,
            workspace_scope_id=workspace_scope_id,
            worker_id=worker_id,
            search=search,
            limit=limit,
            offset=offset,
        )
        return AdminSandboxListResponse(
            sandboxes=[sandbox_summary(route) for route in routes],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/v1/admin/execs", response_model=AdminExecListResponse)
    async def admin_list_execs(
        _: InternalAuth,
        sandbox_id: str | None = Query(default=None, max_length=128),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
    ) -> AdminExecListResponse:
        queries = _fleet_or_raise()
        limit, offset = clamp_page(limit, offset)
        rows, total = await queries.list_execs(
            sandbox_id=sandbox_id, status=status, limit=limit, offset=offset
        )
        return AdminExecListResponse(
            execs=[exec_summary(row) for row in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @app.get(
        "/api/v1/admin/execs/{sandbox_id}/{exec_id}",
        response_model=AdminExecDetail,
    )
    async def admin_exec_detail(sandbox_id: str, exec_id: str, _: InternalAuth) -> AdminExecDetail:
        """One execution, read from the record rather than from the sandbox.

        `GET /api/v1/sandboxes/{id}/exec/{exec_id}` answers for a live sandbox,
        because it is a client polling its own command. An operator is usually
        reading about a sandbox that has been released, and that route refuses
        those with `409 STALE_SANDBOX_ROUTE`, so the audit trail would be
        unreadable exactly when it is wanted. This reads the stored row, which
        is what an audit is.
        """
        row = await database.get_exec(sandbox_id, exec_id)
        if row is None:
            raise HTTPException(status_code=404, detail="EXEC_NOT_FOUND")
        return exec_detail(row)

    @app.get("/admin", include_in_schema=False)
    async def admin_console() -> Response:
        """Serve the console itself.

        Unauthenticated on purpose: this returns only static assets, and every
        API call the page makes carries the operator's token. Requiring auth
        here would mean putting the token in a URL or a cookie, both of which
        are worse.
        """
        return Response(content=console_html(), media_type="text/html; charset=utf-8")

    @app.get("/api/v1/sandboxes/diagnostics/runtime")
    async def fleet_runtime(_: InternalAuth) -> dict[str, object]:
        return await registry.runtime_snapshot()

    @app.get("/internal/v1/health")
    async def worker_health(_: InternalAuth) -> dict[str, object]:
        disk = service.runtime.disk_status()
        return {
            "status": "UP" if disk["available"] else "DRAINING",
            "worker_id": service.worker_id,
            "worker_epoch": service.worker_epoch,
            "capacity": config.worker_capacity,
            "running_sessions": len(service.runtime.sandboxes),
            "running_execs": len(service.runtime.processes),
            "disk": disk,
        }

    @app.post("/internal/v1/sandboxes/{sandbox_id}")
    async def create_local_sandbox(
        sandbox_id: str,
        request: CreateSandboxRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, object]:
        if worker_id != service.worker_id or request.worker_epoch != service.worker_epoch:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        if request.profile != config.profile_id:
            raise HTTPException(status_code=409, detail="SANDBOX_PROFILE_UNAVAILABLE")
        route = await service.validate_local_route(sandbox_id, request.generation)
        if route.sandbox_uid != request.sandbox_uid or route.profile_id != request.profile:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        sandbox = await service.runtime.create(sandbox_id, request.generation, request.sandbox_uid)
        await database.mark_route_ready(
            sandbox_id,
            request.generation,
            service.worker_id,
            service.worker_epoch,
        )
        return {
            "sandbox_id": sandbox_id,
            "generation": sandbox.generation,
            "uid": sandbox.uid,
            "status": "READY",
        }

    @app.get("/internal/v1/sandboxes/{sandbox_id}")
    async def local_status(
        sandbox_id: str,
        generation: int,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, object]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation)
        sandbox = service.runtime.get(sandbox_id, generation)
        return {"sandbox_id": sandbox_id, "generation": sandbox.generation, "status": "READY"}

    @app.post("/internal/v1/sandboxes/{sandbox_id}/exec", response_model=ExecResponse)
    async def exec_command(
        sandbox_id: str,
        request: ExecRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> ExecResponse:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        return await service.execute(sandbox_id, request)

    @app.get("/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}", response_model=ExecResponse)
    async def exec_status(
        sandbox_id: str,
        exec_id: str,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
        generation: Annotated[int | None, Header(alias="X-Sandbox-Generation")] = None,
    ) -> ExecResponse:
        if worker_id != service.worker_id or generation is None:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation)
        row = await database.get_exec(sandbox_id, exec_id)
        if row is None:
            raise HTTPException(status_code=404, detail="EXEC_NOT_FOUND")
        return _exec_from_row(row)

    @app.post("/internal/v1/sandboxes/{sandbox_id}/exec/{exec_id}/cancel")
    async def cancel_exec(
        sandbox_id: str,
        exec_id: str,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
        generation: Annotated[int | None, Header(alias="X-Sandbox-Generation")] = None,
    ) -> dict[str, object]:
        if worker_id != service.worker_id or generation is None:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation)
        return {"cancelled": await service.runtime.cancel(sandbox_id, exec_id)}

    @app.put("/internal/v1/sandboxes/{sandbox_id}/files")
    async def write_file(
        sandbox_id: str,
        request: FileWriteRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, str]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        sandbox = service.runtime.get(sandbox_id, request.generation)
        await service.runtime.write_file(sandbox, request.path, request.content_base64)
        return {"status": "ok", "path": request.path}

    @app.get("/internal/v1/sandboxes/{sandbox_id}/files", response_model=FileReadResponse)
    async def read_file(
        sandbox_id: str,
        path: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> FileReadResponse:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation)
        sandbox = service.runtime.get(sandbox_id, generation)
        encoded = await service.runtime.read_file(sandbox, path)
        return FileReadResponse(path=path, content_base64=encoded)

    @app.get(
        "/internal/v1/sandboxes/{sandbox_id}/files/list",
        response_model=DirectoryListResponse,
    )
    async def list_directory(
        sandbox_id: str,
        path: str,
        generation: Annotated[int, Query(ge=1)],
        _: InternalAuth,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> DirectoryListResponse:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation)
        sandbox = service.runtime.get(sandbox_id, generation)
        bounded = min(limit, config.max_list_entries)
        entries, total, has_more = await _directory_or_raise().list_directory(
            sandbox, path, limit=bounded, offset=offset
        )
        return DirectoryListResponse(
            path=path,
            entries=[DirectoryEntry(**entry) for entry in entries],
            total=total,
            has_more=has_more,
        )

    @app.post("/internal/v1/sandboxes/{sandbox_id}/files/mkdir")
    async def make_directory(
        sandbox_id: str,
        request: MakeDirectoryRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, str]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        sandbox = service.runtime.get(sandbox_id, request.generation)
        await _directory_or_raise().make_directory(sandbox, request.path, parents=request.parents)
        return {"status": "ok", "path": request.path}

    @app.post("/internal/v1/sandboxes/{sandbox_id}/files/delete")
    async def delete_path(
        sandbox_id: str,
        request: DeletePathRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, str]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        sandbox = service.runtime.get(sandbox_id, request.generation)
        await _directory_or_raise().delete_path(sandbox, request.path, recursive=request.recursive)
        return {"status": "ok", "path": request.path}

    @app.post("/internal/v1/sandboxes/{sandbox_id}/files/move")
    async def move_path(
        sandbox_id: str,
        request: MovePathRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, str]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        sandbox = service.runtime.get(sandbox_id, request.generation)
        await _directory_or_raise().move_path(
            sandbox, request.source, request.destination, overwrite=request.overwrite
        )
        return {"status": "ok", "source": request.source, "destination": request.destination}

    @app.post("/internal/v1/sandboxes/{sandbox_id}/templates", response_model=TemplateResponse)
    async def build_template(
        sandbox_id: str,
        request: TemplateBuildRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> TemplateResponse:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        # Archiving is CPU- and IO-bound; keep it off the event loop.
        record = await complete_in_thread(service.build_template, sandbox_id, request)
        return TemplateResponse(**record.as_dict())

    @app.put("/internal/v1/sandboxes/{sandbox_id}/templates")
    async def attach_templates(
        sandbox_id: str,
        request: TemplateAttachRequest,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, object]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, request.generation)
        attached = await complete_in_thread(service.attach_templates, sandbox_id, request)
        return {"status": "ATTACHED", "templates": attached}

    @app.delete("/internal/v1/sandboxes/{sandbox_id}")
    async def destroy_local(
        sandbox_id: str,
        generation: int,
        _: InternalAuth,
        worker_id: Annotated[str | None, Header(alias="X-Sandbox-Worker-ID")] = None,
    ) -> dict[str, str]:
        if worker_id != service.worker_id:
            raise HTTPException(status_code=409, detail="STALE_SANDBOX_ROUTE")
        await service.validate_local_route(sandbox_id, generation, allow_releasing=True)
        await service.runtime.destroy(sandbox_id)
        return {"status": "DESTROYED"}

    # Keep the existing REST and Worker protocols unchanged. The official MCP
    # SDK app is mounted last so it cannot shadow existing paths.
    app.mount("", mcp_asgi, name="mcp")

    return app


app = create_app()
