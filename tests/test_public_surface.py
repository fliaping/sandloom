"""The surface that answers without a token, and what the schema advertises.

Authentication is applied route by route, as a dependency each handler asks for.
That makes the set of paths that answer *without* one a security-relevant
inventory rather than an accident of which decorator remembered: `/admin`,
`/health` and `/healthz` are public on purpose — a page of static assets, a
liveness probe, and the status document — and a handler added without the
dependency would be a public endpoint nobody decided to publish.

The schema served at `/openapi.json`, and rendered at `/docs`, is the API
reference a reader opens first, so every client route has to be in it. One
observation is checked rather than asserted away: the worker protocol under
`/internal/v1` also appears there. Whether a published schema should describe the
protocol replicas use to talk to each other is a product decision — it is
documented in the README either way — so this file pins the client routes and
leaves that one visible.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings
from agent_sandbox.mcp_server import MCP_PROXY_PATH

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Decided, not discovered: a page of static assets, a liveness probe, and the
# status document. Everything else needs the internal token.
PUBLIC = {"/admin", "/health", "/healthz"}

# FastAPI's own routes. They describe the API rather than being part of it, so
# there is nothing to authenticate, and they are what makes the schema usable.
FRAMEWORK = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _app(tmp_path: Path) -> Any:
    return create_app(
        Settings(
            internal_token=TOKEN,
            local_root=tmp_path,
            min_free_bytes=0,
            advertise_host="worker.test",
        )
    )


def _public_paths(app: Any) -> set[str]:
    """Application routes that do not depend on the internal token."""

    open_paths = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = {getattr(dep.call, "__name__", "") for dep in route.dependant.dependencies}
        if "require_internal_auth" not in calls:
            open_paths.add(route.path)
    return open_paths


def test_the_paths_that_answer_without_a_token_are_the_three_that_should(
    tmp_path: Path,
) -> None:
    """A new route that forgets the dependency is a new public endpoint."""

    app = _app(tmp_path)

    assert _public_paths(app) == PUBLIC


def test_the_documented_framework_paths_are_the_ones_served(tmp_path: Path) -> None:
    """The schema and the two UIs are the whole of the non-application surface."""

    app = _app(tmp_path)
    others = {
        getattr(route, "path", "?")
        for route in app.routes
        if not isinstance(route, APIRoute)
        # The MCP application is mounted at the root and carries its own bearer
        # check inside; it is not an unauthenticated path.
        and getattr(route, "path", "?") != ""
    }

    assert others == FRAMEWORK


async def test_an_unknown_path_is_answered_by_the_mount_and_not_by_a_404(
    tmp_path: Path,
) -> None:
    """Which is why a mistyped URL reads as an authentication failure.

    Without a token the answer is 401 for a path that does not exist; with one it
    is a 404. The service therefore never tells an unauthenticated caller whether
    a path exists, at the cost of a typo in a browser looking like a token
    problem.
    """

    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        anonymous = await client.get("/no/such/path")
        authenticated = await client.get("/no/such/path", headers=AUTH)

    assert anonymous.status_code == 401
    assert "internal service authentication failed" in anonymous.text
    assert authenticated.status_code == 404


async def test_the_schema_is_served_and_identifies_the_service(tmp_path: Path) -> None:
    """A client generator needs a title and a version, not just paths."""

    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"]
    assert schema["info"]["version"]


def test_every_client_route_is_described_by_the_schema(tmp_path: Path) -> None:
    """A client route hidden from the schema is a client route nobody can find.

    `/admin` is excluded on purpose — it returns a page, not an API — and that is
    the only exclusion the public API should have.
    """

    app = _app(tmp_path)
    documented = app.openapi()["paths"]

    client_paths = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1/")
    }

    assert client_paths, "the app no longer serves any /api/v1 route"
    assert not sorted(path for path in client_paths if path not in documented)


@pytest.mark.parametrize("path", sorted(PUBLIC))
async def test_the_public_paths_need_no_token(tmp_path: Path, path: str) -> None:
    """Stated as the user experiences it, so the inventory cannot be a fiction."""

    app = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path)

    assert response.status_code == 200, response.text


# ── the paths the documentation names ──

ROOT = Path(__file__).resolve().parents[1]

_DOCUMENTATION = [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]
_DOCUMENTATION += sorted((ROOT / "docs").glob("*.md"))

_PATH_IN_PROSE = re.compile(r"/api/v1/[A-Za-z0-9_.\-{}/,]*")


def _documented_paths() -> dict[str, list[str]]:
    """Every `/api/v1/...` path the markdown names, and where it names it."""

    found: dict[str, list[str]] = {}
    for path in _DOCUMENTATION:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _PATH_IN_PROSE.findall(line):
                found.setdefault(match.rstrip("/.,;:`"), []).append(f"{path.name}:{number}")
    return found


def _segments(path: str) -> list[str]:
    """`/api/v1/sandboxes/{id}/exec` -> `['api', 'v1', 'sandboxes', '*', 'exec']`."""

    return ["*" if part.startswith("{") else part for part in path.strip("/").split("/")]


def _served(documented: str, routes: set[str]) -> bool:
    """Whether some route serves this path, with the documentation's own licence.

    The documents write concrete examples (`/api/v1/sandboxes/my-agent/exec`),
    brace groups (`/api/v1/sandboxes/{id}/files/{mkdir,delete,move}`), and the
    prefix a set of routes lives under (`/api/v1/admin`), so a documented path
    may be narrower or broader than a route and still be about it. What it does
    not license is a different word in the same position: `/api/v1/sandbox`
    matches nothing.
    """

    parts = _segments(documented)
    for route in routes:
        route_parts = _segments(route)
        if len(parts) > len(route_parts):
            continue
        # Only the documented path's segments are compared; a documented prefix
        # matches the longer route it names, which is how `/api/v1/admin` is
        # written as the section its routes live under.
        if all(
            documented_part == route_part or "*" in (documented_part, route_part)
            for documented_part, route_part in zip(parts, route_parts, strict=False)
        ):
            return True
    return False


def test_every_api_path_the_documentation_names_is_one_the_app_serves(tmp_path: Path) -> None:
    """A path a reader copies out of a document has to be a path that exists.

    The direction from a route to the schema is pinned above; this is the half a
    reader experiences. A withdrawn route leaves the documents pointing at
    something that does not answer, and the reader cannot tell that from a token
    problem: an unknown path answers 401 without a token.

    What this proves is bounded, and worth stating: every documented path is
    spelled the way some route is spelled, segment for segment. It does not
    validate the concrete example values -- `/api/v1/sandboxes/my-agent/exec` is
    served by `/api/v1/sandboxes/{sandbox_id}/exec`, and a different word in that
    position would match it too, because that is what a path parameter is. The
    MCP endpoint is added by hand because it is a mount dispatching on its own
    path rather than a route in the table.
    """

    app = _app(tmp_path)
    routes = {
        route.path for route in app.routes if getattr(route, "path", "").startswith("/api/v1/")
    }
    routes.add(MCP_PROXY_PATH)

    documented = _documented_paths()
    assert len(documented) >= 20, f"the documented paths were not found: {sorted(documented)}"

    unserved = {path: where for path, where in documented.items() if not _served(path, routes)}

    assert not unserved, "the documentation names paths nothing serves: " + "; ".join(
        f"{path} ({', '.join(where[:2])})" for path, where in sorted(unserved.items())
    )
