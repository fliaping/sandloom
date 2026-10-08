"""The HTTP status a rejected request answers with.

A status code is a promise about who should act next. 4xx says the caller must
change something; 5xx says the caller may retry. Getting that backwards
produces a retry loop that can never succeed, and a client cannot tell the
difference from the body alone.

The mapping is tested directly rather than over HTTP. The codes are raised from
three different places — the template manager, the runtime, and the service —
and reaching any of them through a real request needs a live worker, so an
HTTP-level test would mostly be testing the fixture. `status_for_error` is the
whole policy, and it is a pure function.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_sandbox.app import create_app, status_for_error
from agent_sandbox.config import Settings
from agent_sandbox.templates import TemplateError

AUTH = {"Authorization": "Bearer test-token"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        internal_token="test-token",
        local_root=tmp_path / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        database_auto_ddl=True,
        profile_hash="profile-a",
        advertise_host="worker.test",
    )


# ── the mapping ──


@pytest.mark.parametrize(
    "code",
    [
        # A path that is not there is a typo, not a worker that is briefly
        # unable to serve. This one is raised by the runtime as a plain
        # RuntimeError, which is exactly why it is easy to leave at the default.
        "SANDBOX_TEMPLATE_SOURCE_NOT_A_DIRECTORY",
    ],
)
def test_a_bad_request_reads_as_a_client_error(code: str) -> None:
    assert status_for_error(RuntimeError(code)) == 400


def test_a_template_error_reads_as_a_client_error() -> None:
    """Missing, corrupt, and oversized are all properties of the request."""
    assert status_for_error(TemplateError("SANDBOX_TEMPLATE_NOT_FOUND")) == 400


def test_a_missing_capability_reads_as_not_implemented() -> None:
    """Retrying cannot make a backend grow a feature it does not have.

    The API's other capability gaps answer 501, so a caller that handles one
    handles all of them.
    """
    assert status_for_error(RuntimeError("SANDBOX_TEMPLATES_UNSUPPORTED")) == 501


def test_capacity_pressure_reads_as_retry_later() -> None:
    assert status_for_error(RuntimeError("SANDBOX_PARALLEL_EXEC_LIMIT")) == 429


@pytest.mark.parametrize(
    "code",
    [
        "STALE_SANDBOX_GENERATION",
        "STALE_SANDBOX_ROUTE",
        "SANDBOX_EXEC_SCOPE_BUSY",
        "SANDBOX_WORKSPACE_LOST",
    ],
)
def test_ownership_conflicts_read_as_conflict(code: str) -> None:
    """The caller has to resolve again or wait; retrying the same call cannot work."""
    assert status_for_error(RuntimeError(code)) == 409


@pytest.mark.parametrize(
    "code",
    [
        "SANDBOX_WORKER_UNREACHABLE",
        "SANDBOX_WORKER_DISK_PRESSURE",
        "NO_SANDBOX_WORKER_AVAILABLE",
        "SANDBOX_WORKER_INVALID_RESPONSE",
    ],
)
def test_worker_state_reads_as_temporarily_unavailable(code: str) -> None:
    """These really are transient, so 503 is the honest answer."""
    assert status_for_error(RuntimeError(code)) == 503


def test_an_unknown_code_does_not_claim_the_request_was_wrong() -> None:
    """Defaulting to 4xx would tell a caller to stop retrying something transient."""
    assert status_for_error(RuntimeError("SANDBOX_SOMETHING_NEW")) == 503


# ── the routes around them ──


async def test_template_routes_require_the_token(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listing = await client.get("/api/v1/templates", headers={"Authorization": "Bearer nope"})
        build = await client.post(
            "/api/v1/sandboxes/sb-1/templates",
            headers={"Authorization": "Bearer nope"},
            json={"generation": 1, "name": "demo"},
        )

    assert listing.status_code == 401
    assert build.status_code == 401


async def test_an_unsupported_fleet_query_answers_not_implemented(tmp_path: Path) -> None:
    """The same 501 contract the template case follows."""
    from agent_sandbox import app as app_module

    original = app_module.as_fleet_queries
    try:
        app_module.as_fleet_queries = lambda _store: None  # type: ignore[assignment]
        app = create_app(_settings(tmp_path))
    finally:
        app_module.as_fleet_queries = original

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH
    ) as client:
        response = await client.get("/api/v1/admin/sandboxes")

    assert response.status_code == 501
    assert "SANDBOX_FLEET_QUERIES_UNSUPPORTED" in response.text
