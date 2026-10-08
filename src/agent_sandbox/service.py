"""Control plane and local worker orchestration."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, cast

import httpx

from .backends import (
    ExecutionBackend,
    as_template_cache_pruning,
    create_execution_backend,
)
from .config import Settings
from .credentials import CredentialBroker, create_credential_broker
from .models import (
    AUDIT_HISTORY_MAX_BYTES,
    AUDIT_HISTORY_MAX_ENTRIES,
    AUDIT_HISTORY_RETENTION_DAYS,
    Route,
)
from .observability import error_code
from .preflight import (
    toolchain_isolation_warnings,
    validate_runtime_environment,
    write_startup_marker,
)
from .registry import Registry
from .runtime import LocalSandbox, configured_toolchains
from .schemas import (
    ExecRequest,
    ExecResponse,
    LifecycleAuditEntry,
    ResolveRequest,
    RouteResponse,
    SandboxAuditResponse,
    TemplateAttachRequest,
    TemplateBuildRequest,
)
from .storage import MetadataStore, as_exec_history_pruning
from .templates import (
    TemplateCatalog,
    TemplateRecord,
    TemplateRef,
    template_object_store,
)

logger = logging.getLogger(__name__)

SYSTEM_REAPER_ACTOR = "system:orphan-reaper"
AUDIT_HISTORY_PRUNE_INTERVAL_SECONDS = 3600

# The codes that mean "this route is not yours to release any more": the
# generation moved, or the route was released. Both answer 409 to a client.
_ROUTE_GONE_CODES = frozenset({"STALE_SANDBOX_GENERATION", "STALE_SANDBOX_ROUTE"})
ReleaseReason = Literal[
    "CLIENT_RELEASE",
    "IDLE_TIMEOUT",
    "WORKER_LOST",
    "PROFILE_UPGRADE",
    "WORKER_REASSIGNED",
    "RELEASE_RETRY",
]


def workspace_actor(route: Route) -> str:
    """Return the strongest caller identity currently available to Sandbox."""
    return f"workspace:{route.workspace_scope_id}"


def route_audit(route: Route) -> SandboxAuditResponse:
    started_at = route.ready_at or route.generation_started_at or route.created_at
    active_duration_ms: int | None = None
    if route.status != "RELEASED" and started_at is not None:
        now = datetime.now(UTC)
        normalized = started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=UTC)
        active_duration_ms = max(0, int((now - normalized).total_seconds() * 1000))
    return SandboxAuditResponse(
        sandbox_id=route.sandbox_id,
        workspace_scope_id=route.workspace_scope_id,
        generation=route.generation,
        status=route.status,
        first_created_at=route.created_at,
        generation_started_at=route.generation_started_at,
        generation_created_by=route.generation_created_by,
        ready_at=route.ready_at,
        last_active_at=route.last_active_at,
        active_duration_ms=active_duration_ms,
        last_released_generation=route.last_released_generation,
        last_released_at=route.last_released_at,
        last_release_reason=route.last_release_reason,
        last_released_by=route.last_released_by,
        last_lifetime_ms=route.last_lifetime_ms,
        lifecycle_count=route.lifecycle_count,
        total_lifetime_ms=route.total_lifetime_ms,
        history_retention_days=AUDIT_HISTORY_RETENTION_DAYS,
        history_max_entries=AUDIT_HISTORY_MAX_ENTRIES,
        history_max_bytes=AUDIT_HISTORY_MAX_BYTES,
        history=[LifecycleAuditEntry.model_validate(entry) for entry in route.lifecycle_history],
    )


class SandboxService:
    def __init__(self, settings: Settings, database: MetadataStore, registry: Registry) -> None:
        self.settings = settings
        self.database = database
        self.registry = registry
        self.runtime: ExecutionBackend = create_execution_backend(settings)
        # Credential brokers are a property of the deployment, not of the
        # execution backend: the base `ExecutionBackend` protocol stays as it
        # was so a plugin written against an earlier release still loads. The
        # default rejects `sensitive_env` outright — the alternative is
        # accepting secrets with nothing configured to handle them.
        self.credential_broker: CredentialBroker = create_credential_broker(settings)
        # The object store is the sharing channel: with one, a template built on
        # any worker resolves on every worker; without one, templates stay local.
        self.template_catalog = TemplateCatalog(
            settings.template_cache_root / "catalog.json",
            object_store=template_object_store(settings),
        )
        self.worker_epoch = uuid.uuid4().hex
        self.worker_id = f"{settings.advertise_host}-{settings.port}"
        self.capabilities: dict[str, object] = {}
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._background_execs: set[asyncio.Task[ExecResponse]] = set()
        self.last_heartbeat_at: float | None = None
        self._last_audit_history_prune_at: float | None = None
        self.reaper_status: dict[str, object] = {
            "last_run_at": None,
            "last_released": 0,
            "released_total": 0,
            "failures_total": 0,
            "already_gone_total": 0,
        }

    async def start(self) -> None:
        self.settings.workspace_root.mkdir(parents=True, exist_ok=True)
        for warning in validate_runtime_environment(self.settings):
            logger.warning("preflight warning: %s", warning)
        # The template channel, prepared before anything can use it. A deployment
        # that names an endpoint and a bucket expects templates to cross workers,
        # and a fresh object store has no bucket in it; finding that out here is
        # one log line, finding it out at the first publish is a boto3 stack
        # trace in a 500.
        await asyncio.to_thread(self._prepare_template_store)
        self.capabilities = await self.runtime.probe()
        for warning in toolchain_isolation_warnings(
            self.capabilities, configured_toolchains(self.settings)
        ):
            logger.warning("preflight warning: %s", warning)
        await self._heartbeat()
        write_startup_marker(
            self.settings.startup_log_path,
            "success",
            f"ready,worker_id={self.worker_id},port={self.settings.port}",
        )
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    def _prepare_template_store(self) -> None:
        """Give the object store the bucket this deployment names, if it lacks it.

        Optional by design: a plugin store, or an install that keeps templates
        worker-local, has nothing to prepare, and a store that cannot be reached
        logs and leaves the failure to the operation that needs it.
        """

        ensure = getattr(self.template_catalog.object_store, "ensure_bucket", None)
        if callable(ensure):
            ensure()

    async def stop(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            await asyncio.gather(self._heartbeat_task, return_exceptions=True)
        if self._maintenance_task:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        for task in self._background_execs:
            task.cancel()
        if self._background_execs:
            await asyncio.gather(*self._background_execs, return_exceptions=True)
        await self.runtime.shutdown()
        try:
            await self.database.upsert_worker(
                worker_id=self.worker_id,
                epoch=self.worker_epoch,
                endpoint=self.settings.worker_endpoint,
                status="OFFLINE",
                running=len(self.runtime.sandboxes),
            )
            await self.registry.unregister(self.worker_id)
        except Exception:
            logger.exception("failed to persist worker shutdown state")

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_seconds)
            try:
                await self._heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker heartbeat failed; retrying next cycle")

    async def _heartbeat(self) -> None:
        disk = self.runtime.disk_status()
        worker_status = "ACTIVE" if disk["available"] else "DRAINING"
        payload: dict[str, Any] = {
            # Freshness is judged by the reader, so stamp every heartbeat.
            "observed_at": int(time.time() * 1000),
            "max_parallel_execs_per_sandbox": self.settings.max_parallel_execs_per_sandbox,
            "worker_id": self.worker_id,
            "worker_epoch": self.worker_epoch,
            "endpoint": self.settings.worker_endpoint,
            "status": worker_status,
            "capacity": self.settings.worker_capacity,
            "running_sessions": len(self.runtime.sandboxes),
            "running_execs": len(self.runtime.processes),
            "profile_id": self.settings.profile_id,
            "profile_hash": self.settings.profile_hash,
            "capabilities": self.capabilities,
            "disk": disk,
        }
        await self.registry.heartbeat(payload)
        await self.database.upsert_worker(
            worker_id=self.worker_id,
            epoch=self.worker_epoch,
            endpoint=self.settings.worker_endpoint,
            status=worker_status,
            running=len(self.runtime.sandboxes),
        )
        self.last_heartbeat_at = time.monotonic()

    async def _maintenance_loop(self) -> None:
        while True:
            try:
                await self._maintenance()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker maintenance failed; retrying next cycle")
            await asyncio.sleep(self.settings.maintenance_interval_seconds)

    async def _maintenance(self) -> None:
        await self.runtime.cleanup_trash()
        # The template cache is the largest thing a worker writes to disk, and
        # disk is what bounds how many sandboxes it can host, so its budget is
        # enforced here rather than left to whoever set the variable. A backend
        # with no local template cache has nothing to reclaim.
        pruning = as_template_cache_pruning(self.runtime)
        if pruning is not None:
            await pruning.prune_template_cache()
        routes = await self.database.list_reapable_routes(
            idle_ttl_seconds=self.settings.idle_ttl_seconds,
            releasing_grace_seconds=self.settings.orphan_release_grace_seconds,
            running_grace_seconds=self.settings.orphan_running_grace_seconds,
            limit=self.settings.orphan_reaper_batch_size,
        )
        released = 0
        # Routes that were gone by the time the sweep reached them: a client
        # released the sandbox, or it was reassigned, between the listing and
        # this call. The sandbox is gone either way, which is what the sweep was
        # there to achieve.
        gone_already = 0
        for route in routes:
            try:
                if route.status == "RUNNING":
                    worker = await self.registry.get(route.worker_id)
                    if (
                        worker
                        and worker.get("worker_id") == route.worker_id
                        and worker.get("worker_epoch") == route.worker_epoch
                    ):
                        continue
                    if not await self.database.begin_worker_lost_release(route):
                        continue
                    await self._finish_release(
                        route,
                        reason="WORKER_LOST",
                        released_by=SYSTEM_REAPER_ACTOR,
                    )
                else:
                    reason: ReleaseReason = (
                        "RELEASE_RETRY" if route.status == "RELEASING" else "IDLE_TIMEOUT"
                    )
                    await self.release(
                        route,
                        reason=reason,
                        released_by=SYSTEM_REAPER_ACTOR,
                    )
                released += 1
                logger.info(
                    "reclaimed orphaned sandbox: sandbox_id=%s generation=%s previous_status=%s",
                    route.sandbox_id,
                    route.generation,
                    route.status,
                )
            except Exception as exc:
                # A stale route is not a failure to reclaim anything: the rest of
                # this service answers 409 for it and means "someone else has it
                # now". Logged as an unhandled exception it was an ERROR with a
                # traceback for every client release that raced the sweep, which
                # is a line an operator learns to skip.
                code = error_code(exc)
                if code in _ROUTE_GONE_CODES:
                    gone_already += 1
                    logger.info(
                        "orphaned sandbox was already gone: sandbox_id=%s generation=%s (%s)",
                        route.sandbox_id,
                        route.generation,
                        code,
                    )
                    continue
                self.reaper_status["failures_total"] = (
                    cast(int, self.reaper_status["failures_total"]) + 1
                )
                logger.exception("failed to reclaim orphaned sandbox: %s", route.sandbox_id)
        self.reaper_status.update(
            {
                "last_run_at": datetime.now(UTC).isoformat(),
                "last_released": released,
                "released_total": cast(int, self.reaper_status["released_total"]) + released,
                "last_already_gone": gone_already,
                "already_gone_total": (
                    cast(int, self.reaper_status["already_gone_total"]) + gone_already
                ),
            }
        )
        monotonic_now = time.monotonic()
        if (
            self._last_audit_history_prune_at is None
            or monotonic_now - self._last_audit_history_prune_at
            >= AUDIT_HISTORY_PRUNE_INTERVAL_SECONDS
        ):
            self._last_audit_history_prune_at = monotonic_now
            try:
                pruned_rows = await self.database.prune_lifecycle_history()
                self.reaper_status["audit_history_pruned_rows"] = pruned_rows
            except Exception:
                logger.exception("audit trail pruning failed; retrying next hour")
            # The recorded executions are the larger half of the audit trail and
            # the only unbounded one: the row holds the output. Same window, same
            # cadence, and a store that keeps its records elsewhere is not asked
            # to delete them -- `as_exec_history_pruning` reports that instead.
            exec_pruning = as_exec_history_pruning(self.database)
            if exec_pruning is not None:
                try:
                    pruned_execs = 0
                    # A pass is bounded, so a backlog takes several. Stop when a
                    # pass deletes nothing, which is the steady state.
                    while True:
                        removed = await exec_pruning.prune_exec_history(
                            older_than_days=AUDIT_HISTORY_RETENTION_DAYS
                        )
                        pruned_execs += removed
                        if removed == 0:
                            break
                    self.reaper_status["exec_history_pruned_rows"] = pruned_execs
                except Exception:
                    logger.exception("execution history pruning failed; retrying next hour")

    @property
    def healthy(self) -> bool:
        if self.last_heartbeat_at is None:
            return False
        return time.monotonic() - self.last_heartbeat_at < self.settings.heartbeat_ttl_seconds

    async def validate_local_route(
        self, sandbox_id: str, generation: int, *, allow_releasing: bool = False
    ) -> Route:
        route = await self.database.find_route(sandbox_id)
        invalid_statuses = {"RELEASED"} if allow_releasing else {"RELEASED", "RELEASING"}
        if (
            route is None
            or route.worker_id != self.worker_id
            or route.worker_epoch != self.worker_epoch
            or route.generation != generation
            or route.status in invalid_statuses
        ):
            raise RuntimeError("STALE_SANDBOX_ROUTE")
        return route

    async def proxy_to_worker(
        self,
        *,
        sandbox_id: str,
        generation: int,
        method: str,
        internal_path: str,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
    ) -> httpx.Response:
        """Forward a unified-entry request to the worker that owns the sandbox.

        A caller only needs ``sandbox_id`` and ``generation``. The fencing headers
        are filled in server-side from the authoritative route, so a client behind
        an HTTP proxy never sees the worker's pod endpoint.
        """
        route = await self.database.find_route(sandbox_id)
        if (
            route is None
            or route.generation != generation
            or route.status in {"RELEASED", "RELEASING"}
        ):
            raise RuntimeError("STALE_SANDBOX_ROUTE")
        worker = await self.registry.get(route.worker_id)
        if (
            not worker
            or worker.get("worker_id") != route.worker_id
            or worker.get("worker_epoch") != route.worker_epoch
        ):
            raise RuntimeError("STALE_SANDBOX_ROUTE")

        endpoint = str(worker["endpoint"]).rstrip("/")
        headers = {
            "Authorization": f"Bearer {self.settings.internal_token}",
            "X-Sandbox-Worker-ID": str(route.worker_id),
            "X-Sandbox-Generation": str(route.generation),
        }
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds, connect=10.0),
                headers=headers,
            ) as client:
                response = await client.request(
                    method,
                    f"{endpoint}{internal_path}",
                    json=json_body,
                    params=params,
                )
                if response.is_success:
                    await self.database.touch_route(sandbox_id, generation)
                return response
        except httpx.RequestError as exc:
            logger.warning(
                "unified API failed to forward to worker sandbox_id=%s worker_id=%s error=%s",
                sandbox_id,
                route.worker_id,
                exc,
            )
            raise RuntimeError("SANDBOX_WORKER_UNREACHABLE") from exc

    async def resolve(self, request: ResolveRequest) -> RouteResponse:
        if request.profile != self.settings.profile_id:
            raise RuntimeError("SANDBOX_PROFILE_UNAVAILABLE")
        route = await self.database.find_route(request.sandbox_id)
        if route is None:
            selected_worker = await self.registry.select(profile_hash=self.settings.profile_hash)
            route = await self.database.create_route(
                sandbox_id=request.sandbox_id,
                workspace_scope_id=request.workspace_scope_id,
                worker=selected_worker,
            )
        if route.workspace_scope_id != request.workspace_scope_id:
            raise RuntimeError("SANDBOX_SCOPE_MISMATCH")
        if route.profile_id != request.profile:
            raise RuntimeError("SANDBOX_PROFILE_MISMATCH")
        if route.profile_hash != self.settings.profile_hash:
            # profile_hash is only a runtime capability fingerprint, so an old route
            # necessarily lags after an upgrade. Rebuild the route (new worker, bumped
            # generation, refreshed hash) instead of returning 409, which would make
            # an existing sandbox_id permanently unresolvable.
            logger.info(
                "sandbox profile fingerprint changed, rebuilding route sandbox_id=%s old=%s new=%s",
                route.sandbox_id,
                route.profile_hash,
                self.settings.profile_hash,
            )
            candidate = await self.registry.select(profile_hash=self.settings.profile_hash)
            route = await self.database.reassign_route(
                route,
                candidate,
                profile_hash=self.settings.profile_hash,
                reason="PROFILE_UPGRADE",
                created_by=f"workspace:{request.workspace_scope_id}",
            )
        online_worker = await self.registry.get(route.worker_id)
        if route.status == "RELEASED":
            candidate = online_worker or await self.registry.select(profile_hash=route.profile_hash)
            route = await self.database.reassign_route(
                route,
                candidate,
                reason="CLIENT_RELEASE",
                created_by=f"workspace:{request.workspace_scope_id}",
            )
            online_worker = await self.registry.get(route.worker_id)
        if not online_worker or online_worker.get("worker_epoch") != route.worker_epoch:
            candidate = online_worker or await self.registry.select(profile_hash=route.profile_hash)
            route = await self.database.reassign_route(
                route,
                candidate,
                reason="WORKER_REASSIGNED",
                created_by=f"workspace:{request.workspace_scope_id}",
            )
            online_worker = await self.registry.get(route.worker_id)
            if not online_worker or online_worker.get("worker_epoch") != route.worker_epoch:
                raise RuntimeError("NO_SANDBOX_WORKER_AVAILABLE")
        return self.route_response(route, online_worker)

    @staticmethod
    def route_response(route: Route, worker: dict[str, Any]) -> RouteResponse:
        return RouteResponse(
            sandbox_id=route.sandbox_id,
            worker_id=str(worker["worker_id"]),
            worker_epoch=str(worker["worker_epoch"]),
            worker_endpoint=str(worker["endpoint"]),
            generation=route.generation,
            sandbox_uid=route.sandbox_uid,
            status=route.status,
            storage_mode=cast("Literal['local', 'shared']", route.storage_mode),
        )

    def _credential_environment(self, request: ExecRequest) -> ExecRequest:
        """Let the deployment's broker configure the environment for this run.

        Both halves happen here, before anything is started, and both come from
        the same object: the broker says whether the caller may ask for the
        sensitive keys it named, and then says what the command should run with.
        Doing it in the service rather than in the execution backend is what
        keeps a third-party backend — which implements the published protocol
        and nothing more — from silently losing credential configuration.
        """

        if request.sensitive_env:
            self.credential_broker.validate_sensitive_keys(set(request.sensitive_env.keys()))
        augmented = self.credential_broker.augment_environment(request, dict(request.env))
        if augmented == request.env:
            return request
        return request.model_copy(update={"env": dict(augmented)})

    async def execute(
        self, sandbox_id: str, request: ExecRequest, *, allow_background: bool = True
    ) -> ExecResponse:
        request = self._credential_environment(request)
        sandbox = self.runtime.get(sandbox_id, request.generation)
        existing = await self.database.begin_exec(
            sandbox_id=sandbox_id,
            exec_id=request.exec_id,
            generation=request.generation,
            worker_id=self.worker_id,
            command=request.model_dump_json(),
            exec_scope=request.exec_scope,
        )
        if existing:
            return _exec_from_row(existing)
        if request.background and allow_background:
            task = asyncio.create_task(self._run_and_persist(sandbox_id, request, sandbox))
            self._background_execs.add(task)
            task.add_done_callback(self._background_execs.discard)
            return ExecResponse(exec_id=request.exec_id, status="RUNNING")
        return await self._run_and_persist(sandbox_id, request, sandbox)

    async def _run_and_persist(
        self, sandbox_id: str, request: ExecRequest, sandbox: LocalSandbox
    ) -> ExecResponse:
        try:
            result = await self.runtime.execute(sandbox, request)
        except asyncio.CancelledError:
            result = ExecResponse(exec_id=request.exec_id, status="CANCELLED")
        except Exception as exc:
            result = ExecResponse(
                exec_id=request.exec_id, status="FAILED", stderr=f"{type(exc).__name__}: {exc}"
            )
        await self.database.finish_exec(
            sandbox_id=sandbox_id,
            exec_id=request.exec_id,
            status=result.status,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            truncated=result.truncated,
        )
        return result

    def _templates_or_raise(self) -> Any:
        """Reject template calls on a backend that cannot serve them."""
        if not getattr(self.runtime, "supports_templates", lambda: False)():
            raise RuntimeError("SANDBOX_TEMPLATES_UNSUPPORTED")
        return self.runtime

    def build_template(self, sandbox_id: str, request: TemplateBuildRequest) -> TemplateRecord:
        """Snapshot a sandbox directory, then publish the name in the catalog.

        Publishing is separate from building so a failed upload never leaves a
        name pointing at a revision no worker can fetch.
        """
        runtime = self._templates_or_raise()
        sandbox = runtime.get(sandbox_id, request.generation)
        record = runtime.build_template(
            sandbox,
            name=request.name,
            source_path=request.source_path,
            description=request.description,
            labels=request.labels,
        )
        return self.template_catalog.publish(record)

    def attach_templates(
        self, sandbox_id: str, request: TemplateAttachRequest
    ) -> list[dict[str, Any]]:
        runtime = self._templates_or_raise()
        runtime.get(sandbox_id, request.generation)
        records = [
            self.template_catalog.resolve(TemplateRef.parse(item)) for item in request.templates
        ]
        attached: list[dict[str, Any]] = runtime.attach_templates(sandbox_id, records)
        return attached

    @property
    def templates_are_shared(self) -> bool:
        """Whether the catalog covers the fleet or only this worker.

        Without an object store a template is published on one worker and known
        only there, so the count on a landing page is not a fleet-wide number.
        Saying which one it is costs a word and prevents a console that shows
        every other figure fleet-wide from implying this one is too.
        """

        return self.template_catalog.object_store is not None

    def list_templates(self) -> list[TemplateRecord]:
        return self.template_catalog.list()

    def remove_template(self, name: str) -> bool:
        """Unpublish a name. Cached revisions stay until the cache prunes them."""
        return self.template_catalog.remove(name)

    async def release(
        self,
        route: Route,
        *,
        reason: ReleaseReason = "CLIENT_RELEASE",
        released_by: str | None = None,
    ) -> None:
        if not await self.database.begin_release(route.sandbox_id, route.generation):
            return
        await self._finish_release(
            route,
            reason=reason,
            released_by=released_by or workspace_actor(route),
        )

    async def _finish_release(
        self,
        route: Route,
        *,
        reason: ReleaseReason,
        released_by: str,
    ) -> None:
        """Destroy the runtime, then finalize an already claimed route."""
        worker = await self.registry.get(route.worker_id)
        worker_matches_route = bool(
            worker
            and worker.get("worker_id") == route.worker_id
            and worker.get("worker_epoch") == route.worker_epoch
        )
        if worker_matches_route:
            assert worker is not None
            headers = {"Authorization": f"Bearer {self.settings.internal_token}"}
            try:
                async with httpx.AsyncClient(timeout=15.0, headers=headers) as client:
                    response = await client.delete(
                        f"{worker['endpoint']}/internal/v1/sandboxes/{route.sandbox_id}",
                        params={"generation": route.generation},
                        headers={
                            "X-Sandbox-Worker-ID": str(worker["worker_id"]),
                            "X-Sandbox-Generation": str(route.generation),
                        },
                    )
                    if response.status_code not in {404, 409}:
                        response.raise_for_status()
            except httpx.RequestError as exc:
                # Stay in RELEASING; a later maintenance pass retries.
                raise RuntimeError("SANDBOX_WORKER_UNREACHABLE") from exc
        elif route.storage_mode == "shared" or route.worker_id == self.worker_id:
            # Once a worker is unreachable, any instance can safely clean a shared
            # workspace, and a restarted instance with the same worker_id can also
            # clean up the local directories it left behind.
            await self.runtime.destroy(route.sandbox_id)
        await self.database.release_route(
            route.sandbox_id,
            route.generation,
            reason=reason,
            released_by=released_by,
        )


def _exec_from_row(row: dict[str, Any]) -> ExecResponse:
    return ExecResponse(
        exec_id=str(row["exec_id"]),
        status=str(row["status"]),
        exit_code=int(row["exit_code"]) if row.get("exit_code") is not None else None,
        stdout=str(row.get("stdout_text") or ""),
        stderr=str(row.get("stderr_text") or ""),
        truncated=bool(row.get("truncated")),
    )
