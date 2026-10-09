"""The internal worker API: who it answers, and what it refuses.

Every route under `/internal/v1` is served by the worker that owns the sandbox and
called by a replica that does not, on behalf of a client whose request landed on
the wrong one. Two headers make that safe: `X-Sandbox-Worker-ID` says which worker
the caller believes it is talking to, and `X-Sandbox-Generation` says which
incarnation of the sandbox. A worker that answers a request addressed to another
worker, or for a generation that has been superseded, acts on a sandbox someone
else owns.

The exec route had a serving-side test. The other thirteen did not, which is
thirteen places the fence could have been left out or moved after the route lookup
without any test noticing — and the order matters: a stale caller must not be able
to make the owner do work on the way to being refused.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings

TOKEN = "test-token"
WORKER = "worker.test-8080"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# (method, path, keyword arguments for the request). The body fields are the
# required ones of each request model, so FastAPI validates before the handler
# runs and a 409 can only come from the fence.
ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "POST",
        "/internal/v1/sandboxes/sb-1",
        {
            "json": {
                "generation": 2,
                "sandbox_uid": 20001,
                "profile": "coding-default",
                "worker_epoch": "epoch-1",
            }
        },
    ),
    ("GET", "/internal/v1/sandboxes/sb-1", {"params": {"generation": 2}}),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/exec",
        {"json": {"exec_id": "exec-1", "generation": 2, "argv": ["/bin/true"]}},
    ),
    (
        "GET",
        "/internal/v1/sandboxes/sb-1/exec/exec-1",
        {"headers": {"X-Sandbox-Generation": "2"}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/exec/exec-1/cancel",
        {"headers": {"X-Sandbox-Generation": "2"}},
    ),
    (
        "PUT",
        "/internal/v1/sandboxes/sb-1/files",
        {"json": {"generation": 2, "path": "/workspace/a.txt", "content_base64": "aGk="}},
    ),
    (
        "GET",
        "/internal/v1/sandboxes/sb-1/files",
        {"params": {"path": "/workspace/a.txt", "generation": 2}},
    ),
    (
        "GET",
        "/internal/v1/sandboxes/sb-1/files/list",
        {"params": {"path": "/workspace", "generation": 2}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/files/mkdir",
        {"json": {"generation": 2, "path": "/workspace/sub"}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/files/delete",
        {"json": {"generation": 2, "path": "/workspace/sub"}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/files/move",
        {
            "json": {
                "generation": 2,
                "source": "/workspace/a.txt",
                "destination": "/workspace/b.txt",
            }
        },
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/templates",
        {"json": {"generation": 2, "name": "env-a", "source_path": "/envs/env-a"}},
    ),
    (
        "PUT",
        "/internal/v1/sandboxes/sb-1/templates",
        {"json": {"generation": 2, "templates": ["env-a"]}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/suspend",
        {"json": {"generation": 2}},
    ),
    (
        "POST",
        "/internal/v1/sandboxes/sb-1/resume",
        {
            "json": {
                "generation": 2,
                "sandbox_uid": 20001,
                "profile": "coding-default",
                "worker_epoch": "epoch-1",
            }
        },
    ),
    (
        "DELETE",
        "/internal/v1/sandboxes/sb-1",
        {"params": {"generation": 2}},
    ),
]

_IDS = [f"{method} {path.split('/sandboxes/')[1]}" for method, path, _ in ROUTES]

# The one internal route that is not addressed to a worker. It reports on the
# worker that answers, so there is no other worker it could be mistaken for; it
# is checked below rather than in the table.
NO_FENCE = {("GET", "/internal/v1/health")}


def _build(tmp_path: Any, **overrides: Any) -> tuple[Any, Any]:
    settings = Settings(
        internal_token=TOKEN,
        local_root=tmp_path,
        min_free_bytes=0,
        advertise_host="worker.test",
        **overrides,
    )
    app = create_app(settings)
    service = app.state.sandbox_service
    # The fence is what these tests are about; whether the route exists in the
    # database is the next question and has its own tests.
    service.validate_local_route = AsyncMock()
    return app, service


def _request(kwargs: dict[str, Any], **extra: str) -> tuple[dict[str, Any], dict[str, str]]:
    """Split a route's arguments into body/params and the headers to send."""

    body = {key: value for key, value in kwargs.items() if key != "headers"}
    return body, {**AUTH, **kwargs.get("headers", {}), **extra}


async def _call(
    app: Any,
    method: str,
    path: str,
    *,
    headers: dict[str, str],
    **kwargs: Any,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_every_internal_route_has_a_row_in_this_table(tmp_path: Any) -> None:
    """A new internal route has to be fenced, and this is how that is noticed.

    The table above is the list of routes whose fence is checked. A route added
    without a row would be a route whose fence nothing verifies — which is how
    twelve of them came to be unverified in the first place — and a row for a
    route that does not exist would silently test a 404.
    """

    app, _ = _build(tmp_path)
    registered = {
        (method, route.path)
        for route in app.routes
        if getattr(route, "path", "").startswith("/internal/v1")
        for method in (getattr(route, "methods", None) or ())
    }

    def template(path: str) -> str:
        """The table names one sandbox and one execution; the app registers patterns."""

        return path.replace("/sandboxes/sb-1", "/sandboxes/{sandbox_id}").replace(
            "/exec/exec-1", "/exec/{exec_id}"
        )

    covered = {(method, template(path)) for method, path, _ in ROUTES} | NO_FENCE

    assert not registered - covered, (
        "these internal routes have no row in ROUTES, so nothing checks their "
        f"fence: {sorted(registered - covered)}"
    )
    assert not covered - registered, (
        f"ROUTES names routes that do not exist: {sorted(covered - registered)}"
    )


def test_the_worker_id_is_the_advertised_host_and_port(tmp_path: Any) -> None:
    """The value a replica has to send, which is what the registry publishes."""

    _, service = _build(tmp_path)

    assert service.worker_id == WORKER


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES, ids=_IDS)
async def test_a_request_addressed_to_another_worker_is_refused(
    tmp_path: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """409, and before the route table is consulted.

    Answering such a request would mean a replica could make this worker act on a
    sandbox that is not its own. Checking the fence after the lookup would still
    refuse, but only after doing the work — and the work is what the fence is for.
    """

    app, service = _build(tmp_path)
    body, headers = _request(kwargs, **{"X-Sandbox-Worker-ID": "worker.other-8080"})
    response = await _call(app, method, path, headers=headers, **body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "STALE_SANDBOX_ROUTE"
    service.validate_local_route.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES, ids=_IDS)
async def test_a_request_with_no_worker_header_is_refused(
    tmp_path: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """Absent is not the same as correct, and must not be treated as "this worker"."""

    app, service = _build(tmp_path)
    body, headers = _request(kwargs)
    response = await _call(app, method, path, headers=headers, **body)

    assert response.status_code == 409, response.text
    service.validate_local_route.assert_not_awaited()


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES, ids=_IDS)
async def test_every_internal_route_requires_the_internal_token(
    tmp_path: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """The internal API is not a second, unauthenticated way in."""

    app, _ = _build(tmp_path)
    body, headers = _request(kwargs)
    headers.pop("Authorization")
    response = await _call(app, method, path, headers=headers, **body)

    assert response.status_code == 401, response.text


@pytest.mark.parametrize(("method", "path", "kwargs"), ROUTES, ids=_IDS)
async def test_a_correctly_addressed_request_reaches_the_route_table(
    tmp_path: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """The fence is not refusing everything: with the right worker id the request
    proceeds to the generation check, which is the next decision."""

    app, service = _build(tmp_path)
    body, headers = _request(kwargs, **{"X-Sandbox-Worker-ID": WORKER})
    if "json" in body and "worker_epoch" in body["json"]:
        # The create route fences on the epoch as well; that fence has its own
        # test below, so this one sends the epoch that is current.
        body["json"]["worker_epoch"] = service.worker_epoch
    await _call(app, method, path, headers=headers, **body)

    assert service.validate_local_route.await_count == 1
    sandbox_id, generation = service.validate_local_route.await_args.args[:2]
    assert sandbox_id == "sb-1"
    assert generation == 2


# The routes that carry the generation in a header rather than in the body.
_BY_HEADER = [entry for entry in ROUTES if "X-Sandbox-Generation" in entry[2].get("headers", {})]


@pytest.mark.parametrize(("method", "path", "kwargs"), _BY_HEADER)
async def test_a_request_without_a_generation_is_refused(
    tmp_path: Any, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    """These two routes have no body, so the generation arrives in a header.

    Without it a worker would answer for whichever generation the sandbox is in
    now, which is how a client holding a stale generation gets a result that
    describes a sandbox it is no longer talking about.
    """

    app, service = _build(tmp_path)
    body, headers = _request(kwargs, **{"X-Sandbox-Worker-ID": WORKER})
    headers.pop("X-Sandbox-Generation")
    response = await _call(app, method, path, headers=headers, **body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == "STALE_SANDBOX_ROUTE"
    service.validate_local_route.assert_not_awaited()


async def test_creating_a_sandbox_is_fenced_by_the_worker_epoch(tmp_path: Any) -> None:
    """The worker id is not enough: a restarted worker has a new epoch.

    A replica that resolved a route before the worker restarted would otherwise
    recreate a sandbox it no longer owns, in a directory the new incarnation may
    already be using.
    """

    app, service = _build(tmp_path)
    response = await _call(
        app,
        "POST",
        "/internal/v1/sandboxes/sb-1",
        headers={**AUTH, "X-Sandbox-Worker-ID": WORKER},
        json={
            "generation": 2,
            "sandbox_uid": 20001,
            "profile": "coding-default",
            "worker_epoch": "epoch-from-before-the-restart",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "STALE_SANDBOX_ROUTE"
    service.validate_local_route.assert_not_awaited()


async def test_creating_a_sandbox_for_another_profile_is_refused(tmp_path: Any) -> None:
    """A replica with different sandbox settings must not place work here.

    Its own profile hash would not match this worker's, so the sandbox it asked
    for is not the sandbox this worker would build.
    """

    app, service = _build(tmp_path)
    response = await _call(
        app,
        "POST",
        "/internal/v1/sandboxes/sb-1",
        headers={**AUTH, "X-Sandbox-Worker-ID": WORKER},
        json={
            "generation": 2,
            "sandbox_uid": 20001,
            "profile": "some-other-profile",
            "worker_epoch": service.worker_epoch,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "SANDBOX_PROFILE_UNAVAILABLE"


async def test_the_internal_listing_cannot_exceed_the_configured_page_bound(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replica asking for a thousand entries gets the page this worker allows.

    The bound belongs to the worker, not to the caller: `SANDBOX_MAX_LIST_ENTRIES`
    exists so one call on a `node_modules` tree cannot build a million-entry
    response, and a peer replica is still a caller.
    """

    from agent_sandbox import app as app_module

    seen: dict[str, int] = {}

    class Directory:
        async def list_directory(
            self, _sandbox: object, _path: str, *, limit: int, offset: int
        ) -> tuple[list[object], int, bool]:
            seen["limit"] = limit
            return [], 0, False

    # Resolved once, when the app is built, from the runtime the service owns.
    monkeypatch.setattr(app_module, "as_directory_operations", lambda _runtime: Directory())
    app, service = _build(tmp_path, max_list_entries=1)
    service.runtime.get = lambda *_args, **_kwargs: object()  # type: ignore[method-assign]

    response = await _call(
        app,
        "GET",
        "/internal/v1/sandboxes/sb-1/files/list",
        headers={**AUTH, "X-Sandbox-Worker-ID": WORKER},
        params={"path": "/workspace", "generation": 2, "limit": 1000},
    )

    assert response.status_code == 200, response.text
    assert seen["limit"] == 1


async def test_the_internal_health_route_has_no_fence(
    tmp_path: Any,
) -> None:
    """The one route with no worker to be addressed to.

    It reports on the worker that answers, so there is no other worker it could be
    mistaken for. It still requires the token, which is what the parametrized
    authentication test above does not cover for this path.
    """

    app, _ = _build(tmp_path)
    response = await _call(app, "GET", "/internal/v1/health", headers=dict(AUTH))

    assert response.status_code == 200, response.text
    assert response.json()["worker_id"] == WORKER
