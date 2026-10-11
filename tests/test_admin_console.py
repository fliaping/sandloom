"""Admin console: fleet queries, read models, and the HTTP surface.

The query tests run against real SQLite rather than a fake store, because the
behavior worth checking — LIKE escaping, grouping, pagination arithmetic, and
how a JSON column round-trips — belongs to the database, not to a mock.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_sandbox.admin import (
    MAX_PAGE_SIZE,
    clamp_page,
    exec_summary,
    sandbox_summary,
    worker_summaries,
)
from agent_sandbox.app import create_app
from agent_sandbox.config import Settings
from agent_sandbox.console import console_html
from agent_sandbox.models import Route
from agent_sandbox.sql_database import SqlAlchemyDatabase
from agent_sandbox.storage import as_fleet_queries

AUTH = {"Authorization": "Bearer test-token"}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "internal_token": "test-token",
        "local_root": tmp_path / "sandboxes",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'control.db'}",
        "database_auto_ddl": True,
        "profile_hash": "profile-a",
        "advertise_host": "worker.test",
    }
    values.update(overrides)
    return Settings(**values)


async def _store(tmp_path: Path) -> SqlAlchemyDatabase:
    pytest.importorskip("aiosqlite")
    database = SqlAlchemyDatabase(_settings(tmp_path))
    await database.connect()
    await database.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )
    return database


async def _seed(database: SqlAlchemyDatabase, sandbox_id: str, scope: str = "tenant/a") -> Route:
    route = await database.create_route(
        sandbox_id=sandbox_id,
        workspace_scope_id=scope,
        worker={"worker_id": "worker-a", "worker_epoch": "epoch-a"},
    )
    await database.mark_route_ready(route.sandbox_id, route.generation, "worker-a", "epoch-a")
    return route


async def _release(database: SqlAlchemyDatabase, route: Route) -> None:
    """Release through both phases, the way the service does."""
    await database.begin_release(route.sandbox_id, route.generation)
    await database.release_route(
        route.sandbox_id, route.generation, reason="CLIENT_RELEASE", released_by="test"
    )


# ── fleet queries against real SQL ──


async def test_listing_pages_and_reports_the_unpaged_total(tmp_path: Path) -> None:
    database = await _store(tmp_path)
    try:
        for index in range(7):
            await _seed(database, f"sandbox-{index}")
        first, total = await database.list_routes(limit=3, offset=0)
        second, _ = await database.list_routes(limit=3, offset=3)

        # The total describes the whole result set, not the page, or the
        # console's pager would never offer a next page.
        assert total == 7
        assert len(first) == 3
        assert len(second) == 3
        assert {route.sandbox_id for route in first}.isdisjoint(
            {route.sandbox_id for route in second}
        )
    finally:
        await database.close()


async def test_filters_narrow_by_status_scope_and_worker(tmp_path: Path) -> None:
    database = await _store(tmp_path)
    try:
        await _seed(database, "sandbox-ready", scope="tenant/a")
        released = await _seed(database, "sandbox-gone", scope="tenant/b")
        await _release(database, released)

        ready, ready_total = await database.list_routes(status="READY")
        assert ready_total == 1
        assert ready[0].sandbox_id == "sandbox-ready"

        scoped, scoped_total = await database.list_routes(workspace_scope_id="tenant/b")
        assert scoped_total == 1
        assert scoped[0].sandbox_id == "sandbox-gone"

        missing, missing_total = await database.list_routes(worker_id="worker-nope")
        assert missing_total == 0
        assert missing == []
    finally:
        await database.close()


async def test_search_treats_wildcards_literally(tmp_path: Path) -> None:
    """A `%` typed into the console must not match every sandbox."""
    database = await _store(tmp_path)
    try:
        await _seed(database, "sandbox-plain")
        await _seed(database, "sandbox-100%-cpu")

        wildcard, wildcard_total = await database.list_routes(search="%")
        assert wildcard_total == 1
        assert wildcard[0].sandbox_id == "sandbox-100%-cpu"

        _, underscore_total = await database.list_routes(search="_")
        assert underscore_total == 0

        substring, substring_total = await database.list_routes(search="plain")
        assert substring_total == 1
        assert substring[0].sandbox_id == "sandbox-plain"
    finally:
        await database.close()


async def test_status_histogram_counts_every_state(tmp_path: Path) -> None:
    database = await _store(tmp_path)
    try:
        await _seed(database, "sandbox-a")
        await _seed(database, "sandbox-b")
        released = await _seed(database, "sandbox-c")
        await _release(database, released)

        histogram = await database.count_routes_by_status()
        assert histogram["READY"] == 2
        assert histogram["RELEASED"] == 1
        assert sum(histogram.values()) == 3
    finally:
        await database.close()


async def test_exec_listing_omits_output_but_keeps_the_command(tmp_path: Path) -> None:
    database = await _store(tmp_path)
    try:
        route = await _seed(database, "sandbox-a")
        await database.begin_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-1",
            generation=route.generation,
            worker_id="worker-a",
            command='{"argv": ["pytest", "-q"], "cwd": "/workspace"}',
            exec_scope="thread-a",
        )
        await database.finish_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-1",
            status="SUCCEEDED",
            exit_code=0,
            stdout="x" * 5000,
            stderr="",
            truncated=False,
        )

        rows, total = await database.list_execs(sandbox_id=route.sandbox_id)
        assert total == 1
        row = rows[0]
        # A listing that inlined megabytes of output would be unusable.
        assert "stdout_text" not in row
        assert "stderr_text" not in row
        assert row["command"]["argv"] == ["pytest", "-q"]
        assert row["exec_scope"] == "thread-a"
        assert row["exit_code"] == 0
    finally:
        await database.close()


async def test_a_recorded_execution_reads_back_after_its_sandbox_is_gone(
    tmp_path: Path,
) -> None:
    """The case an operator actually has: reading about a sandbox that is over.

    The sandbox API answers `409 STALE_SANDBOX_ROUTE` for a released sandbox,
    which is correct for a client polling its own command and useless for an
    audit. The admin route reads the stored row instead, and the stored row has
    the output the listing leaves out.
    """

    app = create_app(_settings(tmp_path))
    database = app.state.sandbox_service.database
    await database.connect()
    try:
        route = await _seed(database, "sandbox-gone")
        await database.begin_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-failed",
            generation=route.generation,
            worker_id="worker-a",
            command='{"argv": ["pytest", "-q"], "cwd": "/workspace"}',
        )
        await database.finish_exec(
            sandbox_id=route.sandbox_id,
            exec_id="exec-failed",
            status="FAILED",
            exit_code=1,
            stdout="collected 3 items\n",
            stderr="E   AssertionError: nope\n",
            truncated=False,
        )
        await _release(database, route)

        rows, total = await database.list_execs(sandbox_id=route.sandbox_id)
        assert total == 1, "the released sandbox no longer has its execution on record"
        assert "stdout_text" not in rows[0], "the listing inlined the output after all"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                f"/api/v1/admin/execs/{route.sandbox_id}/exec-failed", headers=AUTH
            )
            missing = await client.get(
                f"/api/v1/admin/execs/{route.sandbox_id}/exec-never-ran", headers=AUTH
            )
    finally:
        await database.close()

    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["stdout"] == "collected 3 items\n"
    assert detail["stderr"] == "E   AssertionError: nope\n"
    assert detail["exit_code"] == 1
    assert detail["argv"] == ["pytest", "-q"]
    assert detail["status"] == "FAILED"
    # An execution that was never recorded is a 404, not an empty row.
    assert missing.status_code == 404, missing.text


async def test_exec_listing_filters_by_status(tmp_path: Path) -> None:
    database = await _store(tmp_path)
    try:
        route = await _seed(database, "sandbox-a")
        for index, status in enumerate(("SUCCEEDED", "FAILED")):
            await database.begin_exec(
                sandbox_id=route.sandbox_id,
                exec_id=f"exec-{index}",
                generation=route.generation,
                worker_id="worker-a",
                command='{"argv": ["/bin/true"]}',
            )
            await database.finish_exec(
                sandbox_id=route.sandbox_id,
                exec_id=f"exec-{index}",
                status=status,
                exit_code=0 if status == "SUCCEEDED" else 1,
                stdout="",
                stderr="",
                truncated=False,
            )

        failed, total = await database.list_execs(status="FAILED")
        assert total == 1
        assert failed[0]["exec_id"] == "exec-1"
    finally:
        await database.close()


async def test_worker_listing_includes_a_worker_that_stopped_beating(tmp_path: Path) -> None:
    """The dead worker is the one an operator is looking for."""
    database = await _store(tmp_path)
    try:
        await database.upsert_worker(
            worker_id="worker-dead",
            epoch="epoch-dead",
            endpoint="http://worker-dead:8080",
            status="ACTIVE",
            running=3,
        )
        workers = await database.list_workers()
        assert {row["worker_id"] for row in workers} == {"worker-a", "worker-dead"}
    finally:
        await database.close()


# ── read models ──


def _route(**overrides: Any) -> Route:
    base = {
        "sandbox_id": "sandbox-a",
        "workspace_scope_id": "tenant/a",
        "worker_id": "worker-a",
        "worker_epoch": "epoch-a",
        "generation": 2,
        "sandbox_uid": 20001,
        "profile_id": "coding-default",
        "profile_hash": "hash",
        "status": "READY",
        "storage_mode": "local",
    }
    base.update(overrides)
    return Route(**base)  # type: ignore[arg-type]


def test_idle_time_is_reported_for_live_sandboxes_only() -> None:
    now = datetime.now(UTC)
    live = sandbox_summary(_route(last_active_at=now - timedelta(minutes=5)), now=now)
    assert live.idle_seconds is not None
    assert 299 <= live.idle_seconds <= 301

    # A released sandbox has no meaningful idle time, and reporting a growing
    # number would make it look reclaimable when it is already gone.
    released = sandbox_summary(
        _route(status="RELEASED", last_active_at=now - timedelta(days=3)), now=now
    )
    assert released.idle_seconds is None


def test_naive_timestamps_are_treated_as_utc() -> None:
    """Stored timestamps are naive; comparing them to an aware now would raise."""
    now = datetime.now(UTC)
    summary = sandbox_summary(
        _route(last_active_at=(now - timedelta(minutes=2)).replace(tzinfo=None)), now=now
    )
    assert summary.idle_seconds is not None
    assert 119 <= summary.idle_seconds <= 121


def test_idle_time_is_derived_when_no_clock_is_supplied() -> None:
    """The API route calls this without one, so the default is the live path."""

    summary = sandbox_summary(_route(last_active_at=datetime.now(UTC) - timedelta(minutes=3)))

    assert summary.idle_seconds is not None
    assert 179 <= summary.idle_seconds <= 181


def test_a_timestamp_from_the_future_does_not_produce_negative_idle_time() -> None:
    """Timestamps come from a shared database written by other workers.

    A caller whose clock is behind by a few seconds would otherwise see a
    negative idle time, which sorts above everything and reads as nonsense in
    the column an operator uses to find something to reclaim.
    """

    now = datetime.now(UTC)
    summary = sandbox_summary(_route(last_active_at=now + timedelta(seconds=30)), now=now)

    assert summary.idle_seconds == 0


def test_exec_duration_comes_from_timestamps() -> None:
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    summary = exec_summary(
        {
            "sandbox_id": "sandbox-a",
            "exec_id": "exec-1",
            "generation": 1,
            "worker_id": "worker-a",
            "status": "SUCCEEDED",
            "command": {"argv": ["pytest", "-q"]},
            "exit_code": 0,
            "started_at": started,
            "finished_at": started + timedelta(milliseconds=1500),
        }
    )
    assert summary.duration_ms == 1500
    assert summary.argv == ["pytest", "-q"]


def test_a_running_exec_reports_no_duration() -> None:
    """Not zero: zero would read as an instant command that already finished."""
    summary = exec_summary(
        {
            "sandbox_id": "sandbox-a",
            "exec_id": "exec-1",
            "generation": 1,
            "worker_id": "worker-a",
            "status": "RUNNING",
            "command": {"argv": ["sleep", "60"]},
            "started_at": datetime.now(UTC),
            "finished_at": None,
        }
    )
    assert summary.duration_ms is None


def test_exec_summary_survives_a_command_without_argv() -> None:
    summary = exec_summary(
        {
            "sandbox_id": "sandbox-a",
            "exec_id": "exec-1",
            "generation": 1,
            "worker_id": "worker-a",
            "status": "FAILED",
            "command": {},
        }
    )
    assert summary.argv == []


def test_a_worker_missing_from_the_registry_is_not_live() -> None:
    now = datetime.now(UTC)
    rows = [
        {
            "worker_id": "worker-live",
            "worker_epoch": "e1",
            "endpoint": "http://a:8080",
            "status": "ACTIVE",
            "capacity": 32,
            "running_sessions": 4,
            "profile_hash": "hash",
            "heartbeat_at": now - timedelta(seconds=2),
        },
        {
            "worker_id": "worker-dead",
            "worker_epoch": "e2",
            "endpoint": "http://b:8080",
            "status": "ACTIVE",
            "capacity": 32,
            "running_sessions": 9,
            "profile_hash": "hash",
            "heartbeat_at": now - timedelta(hours=2),
        },
    ]
    snapshot = {"workers": [{"worker_id": "worker-live"}]}
    summaries = worker_summaries(rows, snapshot, heartbeat_ttl_seconds=30, now=now)
    live = {item.worker_id: item.live for item in summaries}

    # SQL still holds a row for the dead worker; that disagreement with the
    # registry is the signal, so neither source is allowed to silently win.
    assert live == {"worker-live": True, "worker-dead": False}


def test_a_recent_heartbeat_counts_as_live_without_the_registry() -> None:
    """The in-memory registry knows nothing across replicas; SQL still does."""
    now = datetime.now(UTC)
    rows = [
        {
            "worker_id": "worker-a",
            "worker_epoch": "e1",
            "endpoint": "http://a:8080",
            "status": "ACTIVE",
            "capacity": 8,
            "running_sessions": 1,
            "profile_hash": "hash",
            "heartbeat_at": now - timedelta(seconds=3),
        }
    ]
    summaries = worker_summaries(rows, {"workers": []}, heartbeat_ttl_seconds=30, now=now)
    assert summaries[0].live is True


def test_a_worker_that_has_never_beat_has_no_age() -> None:
    """The row exists as soon as a worker registers; its first beat may not have.

    The Fleet view reads it in the meantime, and an age computed from a missing
    timestamp would raise inside the console rather than render a blank cell.
    """

    now = datetime.now(UTC)
    rows = [
        {
            "worker_id": "worker-just-registered",
            "worker_epoch": "e1",
            "endpoint": "http://a:8080",
            "status": "ACTIVE",
            "capacity": 8,
            "running_sessions": 0,
            "profile_hash": "hash",
            "heartbeat_at": None,
        }
    ]

    summaries = worker_summaries(rows, {"workers": []}, heartbeat_ttl_seconds=30, now=now)

    assert summaries[0].heartbeat_age_seconds is None
    # No beat and no registry entry is not a live worker, however new the row is.
    assert summaries[0].live is False


def test_page_size_is_clamped() -> None:
    assert clamp_page(10_000, 0) == (MAX_PAGE_SIZE, 0)
    assert clamp_page(0, -5) == (1, 0)
    assert clamp_page(25, 50) == (25, 50)


# ── HTTP surface ──


async def test_admin_routes_require_the_internal_token(tmp_path: Path) -> None:
    """Enumerated from the app, so a route added later cannot skip the fence.

    The list used to be written out by hand, and the detail route added after it
    was not in it: the test kept passing while covering three routes of four. A
    hand-maintained list is a second registry a route has to be added to, and
    the one that gets forgotten is the one added last -- so this walks the app's
    own route table, and fails if the walk finds nothing.
    """
    app = create_app(_settings(tmp_path))
    targets = sorted(
        {
            (route.path, method)
            for route in app.routes
            if getattr(route, "path", "").startswith("/api/v1/admin")
            for method in getattr(route, "methods", None) or {"GET"}
            if method not in {"HEAD", "OPTIONS"}
        }
    )
    assert targets, "no admin routes were found, which would make this pass vacuously"
    assert ("/api/v1/admin/execs/{sandbox_id}/{exec_id}", "GET") in targets

    def concrete(path: str) -> str:
        """A request path for a route pattern; the fence runs before routing."""

        return path.replace("{sandbox_id}", "sb-1").replace("{exec_id}", "exec-1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for path, method in targets:
            url = concrete(path)
            anonymous = await client.request(method, url)
            assert anonymous.status_code == 401, (method, path, anonymous.text)
            wrong = await client.request(method, url, headers={"Authorization": "Bearer wrong"})
            assert wrong.status_code == 401, (method, path, wrong.text)


async def test_console_page_is_served_without_a_token(tmp_path: Path) -> None:
    """The page holds no data; requiring auth would force the token into a URL."""
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/admin")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Sandloom fleet console" in response.text


def test_the_fleet_table_shows_the_profile_each_worker_places_sandboxes_for() -> None:
    """The column that explains "no worker available" in a fleet that looks healthy.

    A replica places a sandbox only on a worker whose `SANDBOX_PROFILE_HASH`
    matches its own, so a fleet half rebuilt in place holds two groups of workers
    that both report `live`, both report spare capacity, both answer heartbeats —
    and neither of which will take the other's sandboxes. Every other column the
    table has is identical across the two groups.
    """

    html = console_html()

    assert '"Profile"' in html, "the fleet table no longer shows the profile"
    assert "w.profile_hash" in html, (
        "the profile column no longer reads the worker's profile hash, so a fleet "
        "that is half rebuilt looks like a healthy one again"
    )
    assert '"—"' in html, "a worker that reports no profile has no fallback"
    # Rendered whole. Two hashes can agree for their first twenty characters and
    # still describe different sandboxes — this fleet's own third replica differs
    # from the other two at character twenty-one.
    assert not re.search(r"profile_hash[^,)]*\.slice", html), (
        "the profile hash is truncated, and the part that differs is not always at the front of it"
    )


def test_the_console_reads_an_execution_through_the_admin_route() -> None:
    """The sandbox route refuses a released sandbox, and that is the one an
    operator opens: a console that asked it would show the failure it is most
    often opened to explain as a 409."""

    html = console_html()

    assert '"Read"' in html, "the Executions view offers no way to open one"
    assert "/api/v1/admin/execs/" in html, (
        "the console no longer reads a single execution through the admin route"
    )
    # The sandbox route's shape, which the console must not build any more: it
    # is a call, not the word, so this does not fire on the panel's own prose.
    assert not re.search(r"/sandboxes/\" \+[^;]*\+ \"/exec/\"", html), (
        "the console still builds the sandbox route, which 409s for a released sandbox"
    )
    for field in ("x.stdout", "x.stderr", "x.truncated"):
        assert field in html, f"the panel no longer renders {field}"


def test_execution_selection_is_scoped_to_its_sandbox() -> None:
    html = console_html()
    assert "JSON.stringify([x.sandbox_id, x.exec_id])" in html
    assert "const key = executionKey(x);" in html
    assert html.count("S.execKey === executionKey(x)") == 2
    assert "S.execKey === x.exec_id" not in html


def test_the_console_shows_the_periods_it_reclaims_on() -> None:
    """An operator looking at an idle sandbox is asking when it will be taken.

    The page answered it with nothing at all: the idle time was on every row,
    and the period it is compared against was only in `/healthz`.
    """

    html = console_html()

    assert '"Reclamation"' in html, "the Fleet readout no longer shows the periods"
    assert "d.reclamation" in html, (
        "the readout no longer reads the periods from the payload, so a deployment "
        "that shortened them is described by the shipped defaults"
    )
    assert "rec.maintenance_interval_seconds" in html, (
        "the sweep period is not shown, which is the half that explains an "
        "unreclaimed sandbox in a deployment that shortened the idle TTL"
    )


async def test_overview_reports_the_periods_this_replica_reclaims_on(tmp_path: Path) -> None:
    """Read from settings, not from the release's defaults.

    These describe the replica that answered, like the isolation level and the
    disk figures beside them, and a fleet verified with shortened periods has to
    be able to tell that it is running with them.
    """

    app = create_app(
        _settings(
            tmp_path,
            idle_ttl_seconds=45,
            maintenance_interval_seconds=2,
            orphan_release_grace_seconds=10,
            orphan_running_grace_seconds=7,
            heartbeat_ttl_seconds=10,
        )
    )
    database = app.state.sandbox_service.database
    await database.connect()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/overview", headers=AUTH)
    finally:
        await database.close()

    assert response.status_code == 200, response.text
    assert response.json()["reclamation"] == {
        "maintenance_interval_seconds": 2,
        "heartbeat_interval_seconds": 10,
        "heartbeat_ttl_seconds": 10,
        "idle_ttl_seconds": 45,
        "orphan_running_grace_seconds": 7,
        "orphan_release_grace_seconds": 10,
        "suspended_retention_seconds": 2592000,
        "orphan_dormant_dir_ttl_seconds": 86400,
    }


def test_the_console_never_asks_through_a_host_dialog() -> None:
    """A question a host can answer with a silent no is not a question.

    Measured in an embedded web view: `String(window.confirm("probe"))` returned
    `"false"` immediately, with no dialog and no error. Both destructive actions
    on this page asked that way, so in that host the buttons did nothing at all —
    and said nothing, because the early return is not an error path. Nothing here
    noticed: what these tests check about those buttons is that the API call they
    make names a route that exists, which stays true either way.
    """

    html = console_html()

    # A call, not the name: the page's comments say which dialog it does not use,
    # and a guard that cannot tell prose from a call would have to go without them.
    for name in ("confirm", "alert", "prompt"):
        called = re.search(rf"window\.{name}\s*\(", html)
        assert called is None, (
            f"the console calls window.{name}, which a host may answer with a "
            "silent no, leaving a button that neither acts nor reports"
        )


def test_console_ships_no_external_references() -> None:
    """An operator console must work on an air-gapped network."""
    html = console_html()
    for marker in ("http://", "https://", "//cdn", "<script src", "@import"):
        assert marker not in html, f"console must not reference {marker}"


# `api("<path>", { method: "..." })`: the path is a literal and the method is
# optional (GET). Both are strings in JavaScript, so nothing else connects them
# to the route table.
_CONSOLE_API_CALL = re.compile(r"api\((.*?)\)\s*;", re.DOTALL)
_CONSOLE_PATH = re.compile(r'"(/api/v1/[^"]*)"')
_CONSOLE_METHOD = re.compile(r'method:\s*"([A-Z]+)"')


def _console_api_calls() -> set[tuple[str, str]]:
    """Every (path, method) the console page can call the API with.

    Two shapes have to be covered. A path is either written inline —
    `api("/api/v1/templates/" + encodeURIComponent(name), { method: "DELETE" })`
    — and then the method is spelled out beside it, or it is returned by
    `endpoint()` and passed as `api(endpoint())`, which is the helper's default
    GET. A path that appears inline is bound to the method written with it; the
    rest are GET.
    """

    html = console_html()
    calls: set[tuple[str, str]] = set()
    for body in _CONSOLE_API_CALL.findall(html):
        method = _CONSOLE_METHOD.search(body)
        for path in _CONSOLE_PATH.findall(body):
            calls.add((path, method.group(1) if method else "GET"))

    inline = {path for path, _ in calls}
    for path in _CONSOLE_PATH.findall(html):
        if path not in inline:
            calls.add((path, "GET"))
    return calls


def _registered_routes(tmp_path: Path) -> set[tuple[str, str]]:
    app = create_app(_settings(tmp_path))
    return {
        (route.path, method)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods or ()
    }


def _addresses_a_route(path: str, method: str, routes: set[tuple[str, str]]) -> bool:
    """Whether `path` names a route, allowing for an id appended by the caller.

    The console deletes by appending an id to a path with a trailing slash, so
    `/api/v1/templates/` has to be recognised as the route `/api/v1/templates`
    or `/api/v1/templates/{name}` — but nothing shorter, or a typo in the first
    segment would pass too.
    """

    trimmed = path.split("?", 1)[0].rstrip("/")
    for route_path, route_method in routes:
        if route_method != method:
            continue
        if route_path == trimmed:
            return True
        # The last segment of a templated route is the id the caller appends.
        if route_path.rsplit("/", 1)[0] == trimmed and "{" in route_path:
            return True
    return False


def test_every_api_call_the_console_makes_is_a_route_that_exists(tmp_path: Path) -> None:
    """A renamed route turns a button into a silent no-op.

    The console reaches the API through string literals inside its own script,
    so a route that moves does not fail a type check, a lint, or any test that
    exercises the API directly — the operator clicks the button and nothing
    happens. The worst of these are the two destructive ones, which is exactly
    what a rename would quietly disarm.
    """

    calls = _console_api_calls()
    # A non-vacuous check: a syntax change that stops the parse would otherwise
    # leave this test passing with nothing to check.
    assert len(calls) >= 5, f"only found {calls}, so the extraction is broken"

    routes = _registered_routes(tmp_path)
    unaddressed = sorted(
        f"{method} {path}" for path, method in calls if not _addresses_a_route(path, method, routes)
    )
    assert not unaddressed, f"the console calls routes that do not exist: {unaddressed}"


async def test_sandbox_listing_returns_rows_and_paging_metadata(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    database = app.state.sandbox_service.database
    await database.connect()
    await database.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )
    await _seed(database, "sandbox-alpha")
    await _seed(database, "sandbox-beta")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/admin/sandboxes", headers=AUTH, params={"limit": 1})
    await database.close()

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 2
    assert body["limit"] == 1
    assert len(body["sandboxes"]) == 1
    assert body["sandboxes"][0]["sandbox_id"] in {"sandbox-alpha", "sandbox-beta"}


async def test_overview_reports_counts_and_isolation(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    database = app.state.sandbox_service.database
    await database.connect()
    await database.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )
    await _seed(database, "sandbox-alpha")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/admin/overview", headers=AUTH)
    await database.close()

    body = response.json()
    assert response.status_code == 200
    assert body["sandbox_total"] == 1
    assert body["sandboxes_by_status"]["READY"] == 1
    assert body["fleet_queries_available"] is True
    assert {worker["worker_id"] for worker in body["workers"]} == {"worker-a"}


def _timestamp_fields(value: Any, path: str = "") -> list[tuple[str, str]]:
    """Every string in a response body that reads as an ISO timestamp."""

    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_timestamp_fields(item, f"{path}.{key}" if path else key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_timestamp_fields(item, f"{path}[{index}]"))
    elif isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] == "T":
        found.append((path, value))
    return found


async def test_overview_timestamps_carry_an_offset(tmp_path: Path) -> None:
    """A timestamp without an offset is read as local time, and it is not.

    The database stores naive UTC and hands back `2026-09-30T17:43:53`, which a
    browser eight hours ahead parses as 17:43 local and reports a minute-old
    worker as eight hours old. The offset has to be on the wire: a consumer
    computing a duration has no other way to learn which zone it was in.
    """

    app = create_app(_settings(tmp_path))
    database = app.state.sandbox_service.database
    await database.connect()
    await database.upsert_worker(
        worker_id="worker-a",
        epoch="epoch-a",
        endpoint="http://worker-a:8080",
        status="ACTIVE",
        running=0,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/admin/overview", headers=AUTH)
    await database.close()

    stamps = _timestamp_fields(response.json())
    assert stamps, "the overview carried no timestamps, so this proved nothing"
    for path, value in stamps:
        assert value.endswith("Z") or value.endswith("+00:00"), (
            f"{path} serialized as {value!r}, which has no offset — a client in "
            "any other zone reads the wrong instant"
        )


async def test_overview_says_whether_templates_are_fleet_wide(tmp_path: Path) -> None:
    """A per-worker count must not look like a fleet-wide one.

    Without an object store a template lives on the worker that built it, so the
    number on the landing page describes this worker while every other number on
    it describes the fleet.
    """

    async def overview(settings: Settings) -> dict[str, Any]:
        app = create_app(settings)
        # The ASGI transport does not run the lifespan, so the database the
        # route reads through has to be connected here, as in the other tests.
        database = app.state.sandbox_service.database
        await database.connect()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/v1/admin/overview", headers=AUTH)
        await database.close()
        assert response.status_code == 200
        return response.json()

    assert (await overview(_settings(tmp_path)))["templates_shared"] is False

    # An object store is what makes a published template reachable fleet-wide.
    # It stands in for the configured one so the check stays offline, and the
    # empty catalog is the real "nothing published yet" state.
    from agent_sandbox import templates as templates_module

    class _Store:
        def upload_file(self, key: str, path: str | Path) -> str:
            return f"s3://bucket/{key}"

        def download_to(self, key_or_uri: str, destination: str | Path) -> None:
            raise FileNotFoundError(key_or_uri)

        def delete(self, key_or_uri: str) -> None: ...

    original = templates_module.create_object_store
    try:
        templates_module.create_object_store = lambda _settings: _Store()  # type: ignore[assignment]
        shared = await overview(_settings(tmp_path))
    finally:
        templates_module.create_object_store = original

    assert shared["templates_shared"] is True
    assert shared["template_total"] == 0


async def test_listing_rejects_an_oversized_page(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/admin/sandboxes", headers=AUTH, params={"limit": 100_000}
        )
    assert response.status_code == 422


async def test_a_store_without_fleet_queries_says_so(tmp_path: Path) -> None:
    """A third-party metadata plugin predates these queries and must still boot."""
    from agent_sandbox import app as app_module

    # Simulate a plugin store by removing the capability the console needs.
    original = app_module.as_fleet_queries
    try:
        app_module.as_fleet_queries = lambda _store: None  # type: ignore[assignment]
        degraded = create_app(_settings(tmp_path))
    finally:
        app_module.as_fleet_queries = original

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=degraded), base_url="http://test"
    ) as client:
        listing = await client.get("/api/v1/admin/sandboxes", headers=AUTH)
        overview = await client.get("/api/v1/admin/overview", headers=AUTH)

    # Explicitly unsupported, rather than an empty list that reads as an idle fleet.
    assert listing.status_code == 501
    assert "SANDBOX_FLEET_QUERIES_UNSUPPORTED" in listing.text
    assert overview.status_code == 200
    assert overview.json()["fleet_queries_available"] is False


def test_fleet_narrowing_rejects_a_store_missing_a_method() -> None:
    class Partial:
        async def list_routes(self, **_: Any) -> tuple[list[Route], int]:
            return [], 0

    assert as_fleet_queries(Partial()) is None
    assert as_fleet_queries(None) is None


# ── the page and its reference ──


def _shape(path: str) -> str:
    """A route pattern reduced to segments, for comparing a call to a route.

    The page builds its URLs by concatenation, so `/api/v1/sandboxes/` is half of
    a route and the id it appends is not part of the literal.
    """

    path = path.split("?", 1)[0]
    appends_id = path.endswith("/")
    if appends_id:
        path = path.rstrip("/")
    parts = ["*" if part.startswith("{") else part for part in path.split("/")]
    if appends_id:
        parts.append("*")
    return "/".join(parts)


def _documented_admin_shapes() -> set[str]:
    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "ADMIN_CONSOLE.md").read_text(encoding="utf-8")
    routes = re.findall(r"^\| `(?:GET|POST|DELETE|PUT) (/api/v1/\S*)`", text, re.M)
    assert routes, "no routes were read out of docs/ADMIN_CONSOLE.md"
    return {_shape(route) for route in routes}


def test_every_route_the_console_fetches_is_one_the_reference_documents() -> None:
    """The page and its reference are one contract, checked from the code side.

    A route the console starts calling and a route the table forgot are the same
    defect from two directions: an operator reading the reference cannot
    reproduce what the page does, and cannot see what it is asking for. The page
    is the source of truth here, so a call added without a row fails this rather
    than shipping undocumented.
    """

    roots = sorted(set(re.findall(r'"(/api/v1/[^"]*)"', console_html())))
    assert len(roots) >= 5, f"the console's API calls were not found: {roots}"

    documented = _documented_admin_shapes()
    missing = [
        path
        for path in roots
        if not any(
            shape == _shape(path) or shape.startswith(_shape(path) + "/") for shape in documented
        )
    ]

    assert not missing, (
        f"docs/ADMIN_CONSOLE.md does not document {missing}; the reference needs a "
        "row for every route the page calls"
    )


def _console_view_labels() -> set[str]:
    """The labels the page puts in its navigation."""

    return set(re.findall(r'^\s+\w+: \{ label: "([^"]+)"', console_html(), re.M))


def _documented_views() -> set[str]:
    """The views the reference's "What it shows" table describes."""

    root = Path(__file__).resolve().parents[1]
    text = (root / "docs" / "ADMIN_CONSOLE.md").read_text(encoding="utf-8")
    section = text.split("## What it shows", 1)
    assert len(section) == 2, "docs/ADMIN_CONSOLE.md no longer has a 'What it shows' section"
    return set(re.findall(r"^\| \*\*([^*]+)\*\* \|", section[1].split("\n## ", 1)[0], re.M))


def test_the_documented_views_are_the_views_the_page_renders() -> None:
    """A view is a page an operator navigates to, so both ends have to agree.

    Equality rather than containment, in both directions: a view added without a
    row is undocumented, and a row left behind after a view is removed sends the
    reader looking for something that is not there.
    """

    rendered = _console_view_labels()
    assert len(rendered) >= 4, f"the page's views were not found: {rendered}"

    assert rendered == _documented_views()


# ── the second click ──

# The two actions that destroy something, and the label each carries while it
# waits: the key `act()` is called with, and the text the button switches to.
_DESTRUCTIVE = {"release:": "Confirm release", "unpublish:": "Confirm unpublish"}


@pytest.mark.parametrize(("key", "label"), sorted(_DESTRUCTIVE.items()))
def test_a_destructive_action_says_it_is_waiting_for_a_second_click(key: str, label: str) -> None:
    """The label is the whole feedback: the click that armed it looks like no click.

    Without it the operator cannot tell an armed button from an ignored one, and
    the second click never comes.
    """

    html = console_html()

    assert f'act("{key}" + ' in html, (
        f'no action is dispatched through act("{key}"…), so it either asks through a '
        "host dialog or acts on the first click"
    )
    assert f'"{label}"' in html, f"nothing on the page ever reads {label!r}"


def test_every_delete_the_page_makes_waits_for_its_second_click() -> None:
    """Both destructive calls go through `act`, and nothing bypasses it.

    Checked by counting rather than by naming the two paths, so a third
    destructive action fails here until it is added to the table above -- the
    failure mode being guarded is a `DELETE` sent from a click handler, which
    destroys a workspace or a published name with no second click to wait for.
    """

    html = console_html()

    # `() => api(… { method: "DELETE" })`, allowing the path to be built from a
    # literal plus the id the row carries.
    inside_an_act_callback = re.findall(r'\(\) =>\s*api\([^;]*?method: "DELETE"', html)
    assert len(inside_an_act_callback) == html.count('method: "DELETE"'), (
        "a DELETE is sent outside an act() callback: that one fires on the first "
        "click, with nothing to confirm first"
    )
    assert len(inside_an_act_callback) == len(_DESTRUCTIVE), (
        f"the page sends {len(inside_an_act_callback)} destructive calls and this file "
        f"knows {len(_DESTRUCTIVE)}: add the new one to _DESTRUCTIVE"
    )

    # The labels would survive without this, and the labels are not the guard.
    assert re.search(r"if \(S\.armed !== key\) \{\s*arm\(key\);\s*return;\s*\}", html), (
        "act() no longer arms and returns on the first click, so its label lies"
    )
