from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Self

import httpx
import pytest

from agent_sandbox.config import Settings
from agent_sandbox.models import AUDIT_HISTORY_RETENTION_DAYS, Route
from agent_sandbox.schemas import ExecRequest, ExecResponse, ResolveRequest
from agent_sandbox.service import SandboxService


async def test_resolve_uses_winner_route_after_concurrent_reassignment(tmp_path: Path) -> None:
    old_route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker-old",
        worker_epoch="epoch-old",
        generation=1,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="ASSIGNED",
        storage_mode="shared",
    )
    winner_route = replace(
        old_route,
        worker_id="worker-winner",
        worker_epoch="epoch-winner",
        generation=2,
    )
    candidate = {
        "worker_id": "worker-candidate",
        "worker_epoch": "epoch-candidate",
        "endpoint": "http://candidate:8080",
    }
    winner = {
        "worker_id": "worker-winner",
        "worker_epoch": "epoch-winner",
        "endpoint": "http://winner:8080",
    }

    class FakeDatabase:
        async def find_route(self, _: str) -> Route:
            return old_route

        async def reassign_route(
            self,
            _: Route,
            __: dict[str, Any],
            *,
            profile_hash: str | None = None,
            reason: str,
            created_by: str,
        ) -> Route:
            assert profile_hash is None
            assert reason == "WORKER_REASSIGNED"
            assert created_by == "workspace:workspace-1"
            return winner_route

    class FakeRegistry:
        async def get(self, worker_id: str | None) -> dict[str, Any] | None:
            return winner if worker_id == "worker-winner" else None

        async def select(self, *, profile_hash: str) -> dict[str, Any]:
            assert profile_hash == "profile-hash"
            return candidate

    service = SandboxService(
        Settings(
            internal_token="token",
            profile_hash="profile-hash",
            shared_root=tmp_path,
        ),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        ResolveRequest(
            sandbox_id="sb-123",
            workspace_scope_id="workspace-1",
            profile="coding-default",
        )
    )

    assert response.worker_id == "worker-winner"
    assert response.worker_endpoint == "http://winner:8080"
    assert response.generation == 2


async def test_resolve_reassigns_lost_local_workspace_to_live_worker(tmp_path: Path) -> None:
    old_route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker-old",
        worker_epoch="epoch-old",
        generation=1,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="local",
    )
    reassigned = replace(
        old_route,
        worker_id="worker-new",
        worker_epoch="epoch-new",
        generation=2,
        status="ASSIGNED",
    )
    candidate = {
        "worker_id": "worker-new",
        "worker_epoch": "epoch-new",
        "endpoint": "http://worker-new:8080",
    }

    class FakeDatabase:
        async def find_route(self, _: str) -> Route:
            return old_route

        async def reassign_route(
            self,
            _: Route,
            worker: dict[str, Any],
            *,
            profile_hash: str | None = None,
            reason: str,
            created_by: str,
        ) -> Route:
            assert worker == candidate
            assert profile_hash is None
            assert reason == "WORKER_REASSIGNED"
            assert created_by == "workspace:workspace-1"
            return reassigned

    class FakeRegistry:
        async def get(self, worker_id: str | None) -> dict[str, Any] | None:
            return candidate if worker_id == "worker-new" else None

        async def select(self, *, profile_hash: str) -> dict[str, Any]:
            assert profile_hash == "profile-hash"
            return candidate

    service = SandboxService(
        Settings(
            internal_token="token",
            profile_hash="profile-hash",
            local_root=tmp_path,
        ),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    response = await service.resolve(
        ResolveRequest(
            sandbox_id="sb-123",
            workspace_scope_id="workspace-1",
            profile="coding-default",
        )
    )

    assert response.worker_id == "worker-new"
    assert response.generation == 2
    assert response.storage_mode == "local"


async def test_resolve_rechecks_scope_after_concurrent_create(tmp_path: Path) -> None:
    existing = Route(
        sandbox_id="sb-123",
        workspace_scope_id="another-workspace",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=1,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="ASSIGNED",
        storage_mode="local",
    )

    class FakeDatabase:
        async def find_route(self, _: str) -> None:
            return None

        async def create_route(self, **_: object) -> Route:
            return existing

    class FakeRegistry:
        async def select(self, *, profile_hash: str) -> dict[str, Any]:
            return {
                "worker_id": "worker-1",
                "worker_epoch": "epoch-1",
                "endpoint": "http://worker-1:8080",
            }

    service = SandboxService(
        Settings(internal_token="token", profile_hash="profile-hash", local_root=tmp_path),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="SANDBOX_SCOPE_MISMATCH"):
        await service.resolve(
            ResolveRequest(
                sandbox_id="sb-123",
                workspace_scope_id="workspace-1",
                profile="coding-default",
            )
        )


async def test_maintenance_releases_idle_local_sandbox(tmp_path: Path) -> None:
    route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker.test-8080",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="local",
    )
    events: list[str] = []

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            assert kwargs == {
                "idle_ttl_seconds": 1800,
                "releasing_grace_seconds": 300,
                "running_grace_seconds": 60,
                "limit": 100,
            }
            return [route]

        async def begin_release(self, _: str, __: int) -> bool:
            events.append("begin")
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            assert audit == {
                "reason": "IDLE_TIMEOUT",
                "released_by": "system:orphan-reaper",
            }
            events.append("finish")

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            events.append("trash")

        async def destroy(self, _: str) -> None:
            events.append("destroy")

    class FakeRegistry:
        async def get(self, _: str | None) -> None:
            return None

    settings = Settings(
        internal_token="token",
        profile_hash="profile-hash",
        local_root=tmp_path,
        advertise_host="worker.test",
        port=8080,
    )
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )
    service.worker_epoch = "epoch-1"
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert events == ["trash", "begin", "destroy", "finish"]


async def test_maintenance_prunes_the_template_cache(tmp_path: Path) -> None:
    """`SANDBOX_TEMPLATE_CACHE_MAX_BYTES` promises LRU eviction, and a promise
    the maintenance cycle does not keep is a cache that grows until the disk
    does. Pruning is asserted with the cycle, not on its own, because the
    defect this catches is a correct `prune()` nobody calls."""
    events: list[str] = []

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return []

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            events.append("trash")

        async def prune_template_cache(self) -> list[tuple[str, str]]:
            events.append("prune")
            return [("env", "sha256:deadbeef")]

    settings = Settings(internal_token="token", local_root=tmp_path)
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert events == ["trash", "prune"]


async def test_maintenance_survives_a_backend_that_cannot_prune(tmp_path: Path) -> None:
    """A plugin backend written against an earlier release has no template
    cache to prune, and this must not fail its maintenance cycle."""
    events: list[str] = []

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return []

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            events.append("trash")

    settings = Settings(internal_token="token", local_root=tmp_path)
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert events == ["trash"]


async def test_maintenance_releases_route_owned_by_lost_worker(tmp_path: Path) -> None:
    route = Route(
        sandbox_id="orphan-123",
        workspace_scope_id="workspace-1",
        worker_id="lost-worker-8080",
        worker_epoch="lost-epoch",
        generation=4,
        sandbox_uid=20002,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="ASSIGNED",
        storage_mode="local",
    )
    events: list[str] = []

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **_: object) -> list[Route]:
            return [route]

        async def begin_release(self, _: str, __: int) -> bool:
            events.append("begin")
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            assert audit == {
                "reason": "IDLE_TIMEOUT",
                "released_by": "system:orphan-reaper",
            }
            events.append("finish")

    class FakeRegistry:
        async def get(self, _: str | None) -> None:
            return None

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            events.append("trash")

        async def destroy(self, _: str) -> None:
            raise AssertionError(
                "another unreachable worker's local workspace must not be deleted across instances"
            )

    service = SandboxService(
        Settings(
            internal_token="token",
            profile_hash="profile-hash",
            local_root=tmp_path,
            advertise_host="active-worker",
        ),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert events == ["trash", "begin", "finish"]
    assert service.reaper_status["last_released"] == 1
    assert service.reaper_status["released_total"] == 1


async def test_maintenance_fences_running_exec_after_worker_is_lost(tmp_path: Path) -> None:
    route = Route(
        sandbox_id="running-orphan",
        workspace_scope_id="workspace-1",
        worker_id="lost-worker-8080",
        worker_epoch="lost-epoch",
        generation=5,
        sandbox_uid=20003,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="RUNNING",
        storage_mode="local",
    )
    events: list[str] = []

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **_: object) -> list[Route]:
            return [route]

        async def begin_worker_lost_release(self, _: Route) -> bool:
            events.append("fence-worker-lost-exec")
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            assert audit == {
                "reason": "WORKER_LOST",
                "released_by": "system:orphan-reaper",
            }
            events.append("finish")

    class FakeRegistry:
        async def get(self, _: str | None) -> None:
            return None

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            events.append("trash")

        async def destroy(self, _: str) -> None:
            raise AssertionError(
                "an unreachable worker's local workspace must not be deleted across instances"
            )

    service = SandboxService(
        Settings(
            internal_token="token",
            profile_hash="profile-hash",
            local_root=tmp_path,
            advertise_host="active-worker",
        ),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert events == ["trash", "fence-worker-lost-exec", "finish"]
    assert service.reaper_status["last_released"] == 1


async def test_release_retries_after_live_worker_network_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="shared",
    )
    released = False

    class FakeDatabase:
        async def begin_release(self, _: str, __: int) -> bool:
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            assert audit["reason"] == "CLIENT_RELEASE"
            nonlocal released
            released = True

    class FakeRegistry:
        async def get(self, _: str | None) -> dict[str, str]:
            return {
                "worker_id": "worker-1",
                "worker_epoch": "epoch-1",
                "endpoint": "http://worker-1:8080",
            }

    class FailingClient:
        def __init__(self, **_: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def delete(self, url: str, **_: object) -> httpx.Response:
            raise httpx.ConnectError("unreachable", request=httpx.Request("DELETE", url))

    monkeypatch.setattr("agent_sandbox.service.httpx.AsyncClient", FailingClient)
    service = SandboxService(
        Settings(internal_token="token", shared_root=tmp_path),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="SANDBOX_WORKER_UNREACHABLE"):
        await service.release(route)

    assert released is False


async def test_proxy_rejects_stale_generation_before_contacting_worker(tmp_path: Path) -> None:
    route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="shared",
    )

    class FakeDatabase:
        async def find_route(self, _: str) -> Route:
            return route

    class FakeRegistry:
        async def get(self, _: str | None) -> dict[str, Any]:
            raise AssertionError("a stale generation must not query or reach the worker")

    service = SandboxService(
        Settings(internal_token="token", shared_root=tmp_path),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="STALE_SANDBOX_ROUTE"):
        await service.proxy_to_worker(
            sandbox_id="sb-123",
            generation=2,
            method="GET",
            internal_path="/internal/v1/sandboxes/sb-123/exec/exec-1",
        )


async def test_proxy_adds_authoritative_worker_fencing_headers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    route = Route(
        sandbox_id="sb-123",
        workspace_scope_id="workspace-1",
        worker_id="worker-1",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20001,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="shared",
    )
    worker = {
        "worker_id": "worker-1",
        "worker_epoch": "epoch-1",
        "endpoint": "http://worker-1:8080",
    }
    captured: dict[str, Any] = {}

    class FakeDatabase:
        async def find_route(self, _: str) -> Route:
            return route

        async def touch_route(self, sandbox_id: str, generation: int) -> None:
            captured["touch"] = (sandbox_id, generation)

    class FakeRegistry:
        async def get(self, _: str | None) -> dict[str, Any]:
            return worker

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured["client"] = kwargs

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            captured["request"] = {"method": method, "url": url, **kwargs}
            return httpx.Response(200, json={"status": "ok"})

    monkeypatch.setattr("agent_sandbox.service.httpx.AsyncClient", FakeClient)
    service = SandboxService(
        Settings(internal_token="token", shared_root=tmp_path),
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )

    response = await service.proxy_to_worker(
        sandbox_id="sb-123",
        generation=3,
        method="GET",
        internal_path="/internal/v1/sandboxes/sb-123/exec/exec-1",
    )

    assert response.status_code == 200
    assert captured["client"]["headers"] == {
        "Authorization": "Bearer token",
        "X-Sandbox-Worker-ID": "worker-1",
        "X-Sandbox-Generation": "3",
    }
    assert captured["request"]["url"] == (
        "http://worker-1:8080/internal/v1/sandboxes/sb-123/exec/exec-1"
    )
    assert captured["touch"] == ("sb-123", 3)


class _UnusedDatabase:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the credential checks reached database.{name}")


class _UnusedRegistry:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the credential checks reached registry.{name}")


def _service_without_a_runtime(tmp_path: Path, **overrides: Any) -> SandboxService:
    settings = Settings(
        internal_token="token",
        profile_hash="profile-hash",
        shared_root=tmp_path,
        **overrides,
    )
    return SandboxService(settings, _UnusedDatabase(), _UnusedRegistry())  # type: ignore[arg-type]


async def test_a_request_carrying_secrets_is_refused_before_anything_starts(
    tmp_path: Path,
) -> None:
    """`sensitive_env` is a documented request field, so it must not crash.

    The broker call used to be aimed at `self.runtime.credential_broker`, which
    no backend has, so any request with a non-empty `sensitive_env` died on an
    `AttributeError` and the client got a 500 for asking for a feature the API
    advertises. It is a `ValueError`, which the app answers with 400 and the
    reason, and it is raised before the sandbox is looked up.
    """

    service = _service_without_a_runtime(tmp_path)

    with pytest.raises(ValueError, match="not supported without a credential broker"):
        await service.execute(
            "sb-123",
            ExecRequest(
                exec_id="exec-1",
                generation=1,
                argv=["true"],
                sensitive_env={"GITHUB_TOKEN": "value"},
            ),
        )


def test_the_service_takes_its_broker_from_the_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment chooses the broker; the service must be the one holding it."""

    broker = object()
    seen: list[Settings] = []
    monkeypatch.setattr(
        "agent_sandbox.service.create_credential_broker",
        lambda settings: (seen.append(settings), broker)[1],
    )

    service = _service_without_a_runtime(
        tmp_path, credential_broker="private-credentials"
    )

    assert service.credential_broker is broker
    assert len(seen) == 1 and seen[0].credential_broker == "private-credentials"


class _RecordingRuntime:
    """An execution backend that records the request it was handed."""

    def __init__(self) -> None:
        self.requests: list[ExecRequest] = []

    def get(self, sandbox_id: str, generation: int) -> Any:
        return object()

    async def execute(self, sandbox: Any, request: ExecRequest) -> ExecResponse:
        self.requests.append(request)
        return ExecResponse(exec_id=request.exec_id, status="SUCCEEDED", exit_code=0)


class _RecordingDatabase:
    def __init__(self) -> None:
        self.finished: list[str] = []

    async def begin_exec(self, **_: Any) -> None:
        return None

    async def finish_exec(self, *, exec_id: str, **_: Any) -> None:
        self.finished.append(exec_id)


def _exec_request(**overrides: Any) -> ExecRequest:
    base: dict[str, Any] = {"exec_id": "exec-1", "generation": 1, "argv": ["true"]}
    base.update(overrides)
    return ExecRequest(**base)


async def test_a_broker_can_add_credential_configuration_to_a_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`augment_environment` is the documented way to configure a credential helper.

    It was never called, so a deployment could implement it exactly as
    `docs/ADAPTERS.md` shows and the sandbox would never see a thing. The
    environment it returns now travels to the execution backend, including for a
    backend that implements only the published protocol.
    """

    class Broker:
        def validate_sensitive_keys(self, keys: set[str]) -> None:
            assert keys == set()

        def augment_environment(self, request: ExecRequest, base_env: dict[str, str]) -> dict[str, str]:
            return {
                **base_env,
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "!f() { echo \"$GITHUB_TOKEN\"; }; f",
            }

    monkeypatch.setattr(
        "agent_sandbox.service.create_credential_broker", lambda settings: Broker()
    )
    service = _service_without_a_runtime(tmp_path)
    runtime = _RecordingRuntime()
    database = _RecordingDatabase()
    service.runtime = runtime  # type: ignore[assignment]
    service.database = database  # type: ignore[assignment]

    result = await service.execute(
        "sb-1", _exec_request(env={"LANG": "C.UTF-8"})
    )

    assert result.exit_code == 0
    assert database.finished == ["exec-1"]
    (delivered,) = runtime.requests
    assert delivered.env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert delivered.env["LANG"] == "C.UTF-8", "the caller's own environment was dropped"


async def test_the_default_broker_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The passthrough broker is the default, and it must be invisible."""

    service = _service_without_a_runtime(tmp_path)
    runtime = _RecordingRuntime()
    service.runtime = runtime  # type: ignore[assignment]
    service.database = _RecordingDatabase()  # type: ignore[assignment]

    await service.execute("sb-1", _exec_request(env={"A": "1"}))

    (delivered,) = runtime.requests
    assert delivered.env == {"A": "1"}
    assert delivered is not None


async def test_maintenance_prunes_the_recorded_executions(tmp_path: Path) -> None:
    """The half of the audit trail that grows: the row holds the command output.

    Route lifecycle history was bounded from the start and this was not, so a
    deployment kept every command it had ever run -- 8,700 commands of test load
    left 84 MiB of stdout and stderr against 360 KiB of routes. Asserted with the
    maintenance cycle for the same reason as the template cache: the defect is a
    correct `prune_exec_history()` that nothing calls.
    """
    windows: list[int] = []
    remaining = [3, 3, 0]

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def prune_exec_history(self, *, older_than_days: int) -> int:
            windows.append(older_than_days)
            return remaining.pop(0) if remaining else 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return []

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            return None

    settings = Settings(internal_token="token", local_root=tmp_path)
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    # The window is the audit trail's, not a second number that drifts from it.
    assert windows == [AUDIT_HISTORY_RETENTION_DAYS] * 3
    assert service.reaper_status["exec_history_pruned_rows"] == 6


async def test_maintenance_leaves_a_store_that_cannot_prune_executions_alone(
    tmp_path: Path,
) -> None:
    """A store that keeps its records elsewhere is not asked to delete them."""

    class FakeDatabase:
        pruned = 0

        async def prune_lifecycle_history(self) -> int:
            FakeDatabase.pruned += 1
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return []

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            return None

    settings = Settings(internal_token="token", local_root=tmp_path)
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert FakeDatabase.pruned == 1
    assert "exec_history_pruned_rows" not in service.reaper_status


async def test_a_route_someone_else_released_is_not_a_reclamation_failure(
    tmp_path: Path,
) -> None:
    """The sweep races every client release, and losing the race is not failing.

    A client releases the sandbox between the listing and the sweep's call, so
    the route's generation has moved and the release is refused as stale. That
    is the same 409 a client gets for the same condition, and the sandbox is
    gone either way -- but it was logged as an unhandled exception, so every
    release that raced the sweep put an ERROR and a traceback in the log, and
    `failures_total` counted work that had already been done.
    """
    route = Route(
        sandbox_id="sb-race",
        workspace_scope_id="workspace-1",
        worker_id="worker.test-8080",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20002,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="local",
    )

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return [route]

        async def begin_release(self, _: str, __: int) -> bool:
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            raise RuntimeError("STALE_SANDBOX_GENERATION")

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            return None

        async def destroy(self, _: str) -> None:
            return None

    class FakeRegistry:
        async def get(self, _: str | None) -> None:
            return None

    settings = Settings(
        internal_token="token",
        profile_hash="profile-hash",
        local_root=tmp_path,
        advertise_host="worker.test",
    )
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )
    service.worker_epoch = "epoch-1"
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert service.reaper_status["failures_total"] == 0
    assert service.reaper_status["already_gone_total"] == 1
    assert service.reaper_status["last_already_gone"] == 1
    assert service.reaper_status["last_released"] == 0


async def test_a_reclamation_that_actually_failed_is_still_a_failure(tmp_path: Path) -> None:
    """The classification above must not swallow the real thing."""

    route = Route(
        sandbox_id="sb-broken",
        workspace_scope_id="workspace-1",
        worker_id="worker.test-8080",
        worker_epoch="epoch-1",
        generation=3,
        sandbox_uid=20003,
        profile_id="coding-default",
        profile_hash="profile-hash",
        status="READY",
        storage_mode="local",
    )

    class FakeDatabase:
        async def prune_lifecycle_history(self) -> int:
            return 0

        async def list_reapable_routes(self, **kwargs: object) -> list[Route]:
            return [route]

        async def begin_release(self, _: str, __: int) -> bool:
            return True

        async def release_route(self, _: str, __: int, **audit: str) -> None:
            raise RuntimeError("SANDBOX_STORAGE_UNAVAILABLE")

    class FakeRuntime:
        async def cleanup_trash(self) -> None:
            return None

        async def destroy(self, _: str) -> None:
            return None

    class FakeRegistry:
        async def get(self, _: str | None) -> None:
            return None

    settings = Settings(
        internal_token="token",
        profile_hash="profile-hash",
        local_root=tmp_path,
        advertise_host="worker.test",
    )
    service = SandboxService(
        settings,
        FakeDatabase(),  # type: ignore[arg-type]
        FakeRegistry(),  # type: ignore[arg-type]
    )
    service.worker_epoch = "epoch-1"
    service.runtime = FakeRuntime()  # type: ignore[assignment]

    await service._maintenance()

    assert service.reaper_status["failures_total"] == 1
    assert service.reaper_status["already_gone_total"] == 0
