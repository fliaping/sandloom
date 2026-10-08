#!/usr/bin/env python3
"""Drive a two-replica fleet as a client and check what a fleet promises.

`compose.fleet.yaml` starts the shape README's "More than one replica" describes.
This is what asks whether it behaves the way that section says: a request that
lands on the replica which does not own the sandbox is forwarded rather than
refused, a template built on one worker is mounted by a sandbox on the other, a
superseded generation is rejected instead of answered from stale state, and a
worker that stops answering produces a refusal rather than a wrong answer from
whichever replica picked up the socket.

Every check is made through the *public* client API on a specific replica, so the
fencing headers and the owner's address stay server-side: a client that has to
know which worker owns a sandbox is the failure this is looking for.

Two of the checks need Docker — the bucket that object storage needs, and stopping
a worker — so they are skipped with a reason when the fleet is not reachable
through Compose, and `--strict` turns a skip into a failure:

    export SANDBOX_INTERNAL_TOKEN=$(openssl rand -hex 32)
    SANDBOX_HEARTBEAT_INTERVAL_SECONDS=2 SANDBOX_HEARTBEAT_TTL_SECONDS=10 \
    SANDBOX_MAINTENANCE_INTERVAL_SECONDS=2 SANDBOX_IDLE_TTL_SECONDS=45 \
    SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS=10 \
        docker compose -f compose.fleet.yaml up -d --wait
    uv run python scripts/verify-fleet.py --strict --short-graces
    docker compose -f compose.fleet.yaml down -v

The shortened periods are what makes the checks that wait for a reaper possible
inside a job that has to finish: a fleet that keeps the shipped ones (idle 30
minutes, release grace 5) reclaims nothing while this is running, and those checks
are reported as skipped — `--strict` turns that into a failure, so leaving them
out is a decision rather than an accident.

Exit code is 0 when nothing failed.
"""

from __future__ import annotations

import argparse
import base64
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, cast

import httpx

TOKEN = os.getenv("SANDBOX_INTERNAL_TOKEN", "")
REPLICAS = ("http://127.0.0.1:18081", "http://127.0.0.1:18082")
# The optional third replica, which runs with a different profile hash.
OTHER_PROFILE = "http://127.0.0.1:18083"
COMPOSE_FILE = "compose.fleet.yaml"
TEMPLATE_MOUNT_ROOT = "/envs"


class Failure(Exception):
    """A check that did not hold. Anything else is a bug in this script."""


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


class Client:
    """One replica, as a client with a token sees it."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        headers = {"Authorization": f"Bearer {self.token}"}
        with httpx.Client(timeout=60.0) as client:
            response = client.request(
                method, f"{self.base_url}{path}", json=body, params=params, headers=headers
            )
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, response.text

    def ok(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        status, payload = self.call(method, path, body, params=params)
        expect(status < 300, f"{method} {path} answered {status}: {payload}")
        return payload


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def _record(self, outcome: str, name: str, detail: str) -> None:
        self.rows.append((outcome, name, detail))
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[outcome]
        line = f"[{marker}] {name}"
        if detail:
            line = f"{line:64s} {detail}"
        print(line, flush=True)

    def check(self, name: str, detail: str = "") -> None:
        self._record("pass", name, detail)

    def fail(self, name: str, detail: str) -> None:
        self._record("fail", name, detail)

    def skip(self, name: str, detail: str) -> None:
        self._record("skip", name, detail)

    def run(self, name: str, check: Any) -> None:
        try:
            detail = check() or ""
        except Failure as exc:
            self.fail(name, str(exc))
        except Exception as exc:
            self.fail(name, f"{type(exc).__name__}: {exc}")
        else:
            self.check(name, detail)

    @property
    def failed(self) -> int:
        return sum(1 for outcome, _, _ in self.rows if outcome == "fail")

    @property
    def skipped(self) -> int:
        return sum(1 for outcome, _, _ in self.rows if outcome == "skip")

    @property
    def passed(self) -> int:
        return sum(1 for outcome, _, _ in self.rows if outcome == "pass")


class Fleet:
    """Both replicas, and which worker each of them is."""

    def __init__(self, clients: list[Client]) -> None:
        self.clients = clients
        self.worker_ids = [self._worker_id(client) for client in clients]

    def _worker_id(self, client: Client) -> str:
        status, payload = client.call("GET", "/healthz")
        expect(status == 200, f"{client.base_url}/healthz answered {status}")
        worker = payload["worker"]["id"]
        return str(worker)

    def peer_of(self, worker_id: str) -> Client:
        """The replica that does not own this sandbox."""

        for client, candidate in zip(self.clients, self.worker_ids, strict=True):
            if candidate != worker_id:
                return client
        raise Failure(f"every replica reports worker id {worker_id}")

    def owner_of(self, worker_id: str) -> Client:
        for client, candidate in zip(self.clients, self.worker_ids, strict=True):
            if candidate == worker_id:
                return client
        raise Failure(f"no replica reports worker id {worker_id}")

    def container_for(self, worker_id: str) -> str:
        """The Compose service a worker id belongs to.

        Worker ids are `<advertise_host>-<port>` and the fleet sets the advertise
        host to the service name, so the id is the service name plus a port.
        """

        return worker_id.rsplit("-", 1)[0]


class Sandbox:
    """One sandbox, driven through whichever replica is asked to serve it."""

    def __init__(self, fleet: Fleet, name: str, *, templates: list[str] | None = None) -> None:
        self.fleet = fleet
        self.sandbox_id = f"{name}-{uuid.uuid4().hex[:8]}"
        self.generation = 0
        self.owner = ""
        self.connected = False
        self.templates = list(templates or [])

    def resolve(self, client: Client) -> dict[str, Any]:
        route = client.ok(
            "POST",
            "/api/v1/sandboxes/resolve",
            {"sandbox_id": self.sandbox_id, "workspace_scope_id": f"scope-{self.sandbox_id}"},
        )
        self.generation = int(route["generation"])
        self.owner = str(route["worker_id"])
        return dict(route)

    def connect(self, client: Client) -> None:
        client.ok("POST", f"/api/v1/sandboxes/{self.sandbox_id}", {"generation": self.generation})
        self.connected = True

    def exec(
        self,
        client: Client,
        argv: list[str],
        *,
        generation: int | None = None,
        timeout: int = 60,
    ) -> tuple[int, Any]:
        return client.call(
            "POST",
            f"/api/v1/sandboxes/{self.sandbox_id}/exec",
            {
                "exec_id": f"exec-{uuid.uuid4().hex[:12]}",
                "generation": self.generation if generation is None else generation,
                "argv": argv,
                "timeout_seconds": timeout,
            },
        )

    def release(self) -> None:
        if not self.connected:
            return
        owner = self.fleet.owner_of(self.owner)
        owner.call(
            "DELETE",
            f"/api/v1/sandboxes/{self.sandbox_id}",
            params={"generation": self.generation},
        )
        self.connected = False


# What `--short-graces` asserts about every replica it drives. The checks below
# wait for a reaper, and the shipped periods are minutes long, so they can only
# pass for the right reason when the fleet was started with the shortened ones.
# The numbers are ceilings rather than the documented values, because a fleet
# started with something even shorter is still a fleet these checks can verify.
SHORT_GRACE_LIMITS = {
    "maintenance_interval_seconds": 10.0,
    "heartbeat_ttl_seconds": 30.0,
    "idle_ttl_seconds": 60.0,
    "orphan_release_grace_seconds": 30.0,
}


def check_short_graces(clients: list[Client]) -> str:
    """Read the periods back, rather than trusting the flag that names them.

    Skipping the exports in the documented command is an easy mistake and an
    otherwise silent one: every other check still passes, and the idle cleanup
    check fails two minutes later saying the fleet "needs" a short idle TTL --
    which is exactly what the flag already claimed. Reading the periods back from
    each worker replaces that with one message naming the settings, and it is a
    setup error rather than a failed check, so the caller stops the run.
    """

    described = []
    for client in clients:
        payload = client.ok("GET", "/healthz")
        worker = payload["worker"]
        periods = worker.get("reclamation")
        expect(
            isinstance(periods, dict),
            f"{worker['id']} does not report its reclamation periods; rebuild the worker "
            "image from a tree that has them",
        )
        assert isinstance(periods, dict)
        over = {
            name: periods.get(name)
            for name, limit in SHORT_GRACE_LIMITS.items()
            if not isinstance(periods.get(name), (int, float)) or periods[name] > limit
        }
        expect(
            not over,
            f"{worker['id']} runs with {over}, and --short-graces asserts at most "
            f"{SHORT_GRACE_LIMITS}: start the fleet as docs/OPEN_SOURCE_RELEASE.md "
            "shows, exporting SANDBOX_IDLE_TTL_SECONDS and the rest",
        )
        described.append(", ".join(f"{name}={periods[name]}" for name in SHORT_GRACE_LIMITS))
    return "; ".join(described)


def check_replicas(fleet: Fleet, report: Report) -> None:
    def distinct() -> str:
        expect(
            len(set(fleet.worker_ids)) == len(fleet.worker_ids),
            f"two replicas report one worker id: {fleet.worker_ids}",
        )
        return ", ".join(fleet.worker_ids)

    report.run("fleet: each replica is a distinct worker", distinct)

    def shared_view() -> str:
        views = []
        for client in fleet.clients:
            overview = client.ok("GET", "/api/v1/admin/overview")
            live = sorted(
                str(worker["worker_id"]) for worker in overview["workers"] if worker["live"]
            )
            views.append(live)
        # Equal to each other, and at least the replicas this script is driving:
        # a fleet may have more workers than the two it was pointed at, which is
        # exactly what a third replica of another profile is.
        expect(views[0] == views[1], f"the replicas do not see the same fleet: {views}")
        expect(
            set(fleet.worker_ids) <= set(views[0]),
            f"a replica does not see itself in the fleet view: {views}",
        )
        return f"{len(views[0])} workers, seen the same way by both replicas"

    report.run("fleet: the registry is shared", shared_view)

    def templates_shared() -> str:
        flags = {
            bool(client.ok("GET", "/api/v1/admin/overview")["templates_shared"])
            for client in fleet.clients
        }
        expect(flags == {True}, "a replica does not report templates as shared")
        return "both replicas report one template catalog"

    report.run("fleet: templates are shared rather than worker-local", templates_shared)


def check_forwarding(fleet: Fleet, sandbox: Sandbox, report: Report) -> None:
    def resolve_through_one() -> str:
        route = sandbox.resolve(fleet.clients[0])
        expect(route["generation"] == 1, f"first resolve answered generation {route['generation']}")
        return f"owner {sandbox.owner}"

    report.run("forwarding: resolve assigns an owner", resolve_through_one)

    def visible_from_the_other() -> str:
        peer = fleet.peer_of(sandbox.owner)
        route = peer.ok("GET", f"/api/v1/sandboxes/{sandbox.sandbox_id}")
        expect(
            str(route["worker_id"]) == sandbox.owner,
            f"the other replica reports owner {route['worker_id']}",
        )
        return "the route is in the shared metadata store"

    report.run("forwarding: the other replica can see the route", visible_from_the_other)

    def connect_through_the_peer() -> str:
        peer = fleet.peer_of(sandbox.owner)
        sandbox.connect(peer)
        return f"connected through {peer.base_url}"

    report.run(
        "forwarding: connect through the replica that does not own it", connect_through_the_peer
    )

    def exec_through_the_peer() -> str:
        peer = fleet.peer_of(sandbox.owner)
        status, payload = sandbox.exec(peer, ["/bin/echo", "served-by-the-other-replica"])
        expect(status == 200, f"exec answered {status}: {payload}")
        expect(
            payload["stdout"] == "served-by-the-other-replica\n",
            f"the command did not run on the owner: {payload}",
        )
        return "the request ran on the owner and came back through the peer"

    report.run("forwarding: exec through the replica that does not own it", exec_through_the_peer)

    def files_through_both() -> str:
        peer = fleet.peer_of(sandbox.owner)
        owner = fleet.owner_of(sandbox.owner)
        written = "written-through-the-peer\n"
        peer.ok(
            "PUT",
            f"/api/v1/sandboxes/{sandbox.sandbox_id}/files",
            {
                "generation": sandbox.generation,
                "path": "/workspace/cross.txt",
                "content_base64": base64.b64encode(written.encode()).decode(),
            },
        )
        read = owner.ok(
            "GET",
            f"/api/v1/sandboxes/{sandbox.sandbox_id}/files",
            params={"path": "/workspace/cross.txt", "generation": sandbox.generation},
        )
        expect(
            read.get("content_base64"),
            f"the owner did not return the file the peer wrote: {read}",
        )
        decoded = base64.b64decode(read["content_base64"]).decode()
        expect(decoded == written, f"the file came back as {decoded!r}")
        return "a file written through the peer is read back from the owner"

    report.run("forwarding: the file API crosses replicas", files_through_both)

    def stale_generation() -> str:
        peer = fleet.peer_of(sandbox.owner)
        status, payload = sandbox.exec(
            peer, ["/bin/echo", "stale"], generation=sandbox.generation + 1
        )
        expect(status == 409, f"a superseded generation answered {status}: {payload}")
        expect(
            "STALE_SANDBOX_ROUTE" in str(payload),
            f"the refusal does not name the reason: {payload}",
        )
        return "409 STALE_SANDBOX_ROUTE"

    report.run("forwarding: a superseded generation is refused", stale_generation)


def check_cross_worker_templates(fleet: Fleet, first: Sandbox, report: Report) -> Sandbox | None:
    """A template built on one worker, mounted by a sandbox on the other."""

    name = f"fleet-env-{uuid.uuid4().hex[:6]}"
    state: dict[str, Any] = {}

    def build_and_publish() -> str:
        # Driven through the peer, so the archive is built by the owner and
        # uploaded from there.
        peer = fleet.peer_of(first.owner)
        marker = f"built-on-{first.owner}\n"
        status, payload = first.exec(
            peer,
            [
                "/bin/sh",
                "-c",
                f"mkdir -p /workspace/env && printf '%s' '{marker}' > /workspace/env/marker.txt",
            ],
        )
        expect(status == 200 and payload["exit_code"] == 0, f"building the tree failed: {payload}")
        state["marker"] = marker
        published = peer.ok(
            "POST",
            f"/api/v1/sandboxes/{first.sandbox_id}/templates",
            {
                "generation": first.generation,
                "name": name,
                "source_path": "/workspace/env",
            },
        )
        state["digest"] = published["digest"]
        return f"{name} @ {str(published['digest'])[:22]}…"

    report.run("templates: publish from the peer, built on the owner", build_and_publish)
    if "digest" not in state:
        return None

    def catalog_is_shared() -> str:
        for client in fleet.clients:
            listed = client.ok("GET", "/api/v1/templates")
            names = [entry["name"] for entry in listed["templates"]]
            expect(name in names, f"{client.base_url} does not list {name}: {names}")
        return "both replicas list it"

    report.run("templates: the catalog is shared", catalog_is_shared)

    def lands_on_the_other_worker() -> str:
        """Load decides: the owner already has one sandbox, the other has none."""

        deadline = time.time() + 30
        while time.time() < deadline:
            candidate = Sandbox(fleet, "fleet-second")
            candidate.resolve(fleet.clients[0])
            if candidate.owner != first.owner:
                state["second"] = candidate
                return f"owner {candidate.owner}, not {first.owner}"
            time.sleep(2)
        raise Failure(
            "every resolve landed on the same worker; the fleet is not balancing "
            "between two replicas that both report capacity"
        )

    report.run("templates: a second sandbox lands on the other worker", lands_on_the_other_worker)
    second = state.get("second")
    if second is None:
        return None

    def attach_and_read() -> str:
        peer = fleet.peer_of(second.owner)
        second.connect(peer)
        peer.ok(
            "PUT",
            f"/api/v1/sandboxes/{second.sandbox_id}/templates",
            {"generation": second.generation, "templates": [name]},
        )
        status, payload = second.exec(
            peer, ["/bin/cat", f"{TEMPLATE_MOUNT_ROOT}/{name}/marker.txt"]
        )
        expect(status == 200 and payload["exit_code"] == 0, f"reading the mount failed: {payload}")
        expect(
            payload["stdout"] == state["marker"],
            f"the mount holds {payload['stdout']!r}, not {state['marker']!r}",
        )
        return f"{second.owner} mounted what {first.owner} built"

    report.run("templates: the other worker mounts it and reads the file", attach_and_read)

    def mount_is_read_only() -> str:
        peer = fleet.peer_of(second.owner)
        status, payload = second.exec(
            peer, ["/bin/sh", "-c", f"echo nope > {TEMPLATE_MOUNT_ROOT}/{name}/other.txt"]
        )
        expect(status == 200, f"exec answered {status}: {payload}")
        expect(
            payload["exit_code"] != 0,
            f"the template mount accepted a write: {payload}",
        )
        return "a write into the mount fails"

    report.run("templates: the mount is read-only on the second worker", mount_is_read_only)

    def retirement_reaches_every_replica() -> str:
        """The catalog is shared, so retiring a name has to reach both replicas.

        Publishing through the object store is what makes a template cross
        workers; a delete that only reached the worker it was sent to would
        leave the other replica serving an environment that was retired, and the
        operator has no way to tell from the API they called.
        """
        peer = fleet.peer_of(first.owner)
        peer.ok("DELETE", f"/api/v1/templates/{name}")
        for client in fleet.clients:
            listed = client.ok("GET", "/api/v1/templates")
            names = [entry["name"] for entry in listed["templates"]]
            expect(name not in names, f"{client.base_url} still lists the retired {name}: {names}")
        status, refused = fleet.clients[0].call(
            "PUT",
            f"/api/v1/sandboxes/{second.sandbox_id}/templates",
            body={"generation": second.generation, "templates": [name]},
        )
        expect(
            400 <= status < 500,
            f"a retired name attached again through the other replica: {status} {refused}",
        )
        return "both replicas dropped it, and neither attaches it again"

    report.run("templates: retiring a name reaches every replica", retirement_reaches_every_replica)
    return cast("Sandbox", second)


def check_worker_loss(
    fleet: Fleet,
    sandbox: Sandbox,
    report: Report,
    *,
    compose_file: str,
    project: str,
    token: str,
    reclaim_timeout: float,
    sandboxes: list[Sandbox],
) -> None:
    """A worker that stops answering, seen from the replica that still is.

    Its own sandbox, because the checks before this one run with the idle period
    shortened — that is what the idle check waits for — long enough to reclaim
    theirs. A sandbox the reaper already took proves nothing about a worker that
    stopped.
    """

    sandbox = Sandbox(fleet, "fleet-loss")
    sandboxes.append(sandbox)

    def resolve_and_connect() -> str:
        sandbox.resolve(fleet.clients[0])
        peer = fleet.peer_of(sandbox.owner)
        sandbox.connect(peer)
        return f"owner {sandbox.owner}, driven through {peer.base_url}"

    report.run(
        "worker loss: a sandbox on one worker, driven through the other", resolve_and_connect
    )

    peer = fleet.peer_of(sandbox.owner)
    service = fleet.container_for(sandbox.owner)

    def stop_the_owner() -> str:
        command = [
            "docker",
            "compose",
            "-f",
            compose_file,
            "--project-name",
            project,
            "stop",
            service,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, env=compose_env(token)
        )
        expect(result.returncode == 0, f"{' '.join(command)} failed: {result.stderr.strip()}")
        return f"stopped {service}"

    in_flight: dict[str, Any] = {}

    def start_a_command_that_will_be_interrupted() -> str:
        """One that is running when the worker dies, not one made afterwards.

        The client's connection to the peer stays open across the death of the
        worker behind it, so this is the case a client actually hits during a
        rolling restart: a command already running, and a decision to make about
        what to tell the caller.
        """

        result: dict[str, Any] = {}

        def command() -> None:
            try:
                result["answer"] = sandbox.exec(peer, ["/bin/sleep", "30"], timeout=45)
            except Exception as exc:
                result["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=command, daemon=True)
        thread.start()
        in_flight["thread"] = thread
        in_flight["result"] = result
        deadline = time.time() + 15
        while time.time() < deadline:
            # The exec has to have reached the owner before it is interrupted, or
            # this proves nothing: it is queued, then running.
            time.sleep(1)
            worker = fleet.owner_of(sandbox.owner)
            running = worker.ok("GET", "/healthz")["worker"].get("running_execs")
            if running:
                return f"a 30s command is running on {sandbox.owner}"
            if thread.is_alive() is False:
                raise Failure(f"the command finished before it could be interrupted: {result}")
        raise Failure("the command never appeared as running on the owner")

    report.run(
        "worker loss: start a command and interrupt it", start_a_command_that_will_be_interrupted
    )

    report.run("worker loss: stop the worker that owns the sandbox", stop_the_owner)

    def the_interrupted_command_did_not_answer() -> str:
        thread = in_flight.get("thread")
        if thread is None:
            raise Failure("the command was never started")
        thread.join(timeout=30)
        if thread.is_alive():
            raise Failure("the client is still waiting for a command whose worker is gone")
        result = in_flight["result"]
        if "error" in result:
            return f"the client was told {result['error']}"
        status, payload = result["answer"]
        expect(
            status != 200,
            f"a command whose worker died answered 200: {payload}",
        )
        return f"{status} {str(payload)[:60]}"

    report.run(
        "worker loss: the command in flight fails rather than hanging",
        the_interrupted_command_did_not_answer,
    )

    def refused_not_answered() -> str:
        # The registry entry expires on the heartbeat TTL; until then the peer may
        # still try to reach a stopped container and fail to connect. Both are
        # refusals, and neither is an answer from stale state.
        deadline = time.time() + 60
        seen = ""
        while time.time() < deadline:
            status, payload = sandbox.exec(peer, ["/bin/echo", "should-not-run"])
            seen = f"{status} {payload}"
            if status in {409, 503}:
                return seen
            time.sleep(2)
        raise Failure(f"a sandbox on a stopped worker kept answering: {seen}")

    report.run("worker loss: a request for it is refused", refused_not_answered)

    def gone_from_the_fleet_view() -> str:
        deadline = time.time() + 60
        while time.time() < deadline:
            overview = peer.ok("GET", "/api/v1/admin/overview")
            live = {str(worker["worker_id"]) for worker in overview["workers"] if worker["live"]}
            if sandbox.owner not in live:
                return f"{sandbox.owner} is no longer live in the view"
            time.sleep(2)
        raise Failure(f"{sandbox.owner} is still listed live after its worker stopped")

    report.run("worker loss: the fleet view drops it", gone_from_the_fleet_view)

    def the_sandbox_is_released() -> str:
        """Not left owned by a worker that is gone.

        A route whose worker disappeared is the one state a fleet cannot leave
        alone: nothing will ever answer for it and the capacity it counted against
        is not coming back. Which of the two paths takes it depends on what it was
        doing when the worker stopped — a command was in flight, so this one is
        RUNNING and the reaper releases it as `WORKER_LOST`; a route nobody was
        using is released on the idle path instead. Either is a release, which is
        what the check is about.
        """

        path = f"/api/v1/sandboxes/{sandbox.sandbox_id}"
        deadline = time.time() + reclaim_timeout
        seen = ""
        while time.time() < deadline:
            _, route = peer.call("GET", path)
            seen = str(route.get("status", route)) if isinstance(route, dict) else str(route)
            if seen == "RELEASED":
                audit = peer.ok("GET", f"{path}/audit")
                reason = audit["last_release_reason"]
                expect(
                    reason in {"WORKER_LOST", "IDLE_TIMEOUT"},
                    f"released for {reason}, which is neither the worker-lost path "
                    "nor the idle one",
                )
                return f"released for {reason}"
            time.sleep(2)
        raise Failure(
            f"the sandbox is still {seen} {reclaim_timeout:.0f}s after its worker stopped; "
            "the fleet needs SANDBOX_IDLE_TTL_SECONDS and "
            "SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS short for this to be observable, "
            "and --short-graces to say so"
        )

    report.run("worker loss: the sandbox it owned is released", the_sandbox_is_released)

    def start_it_again() -> str:
        # So the next run starts from a fleet, rather than from one this one left
        # half down. A restarted worker comes back with a new epoch, which the
        # route to its old sandboxes does not match — still a refusal, which is
        # the behavior the checks above assert.
        command = [
            "docker",
            "compose",
            "-f",
            compose_file,
            "--project-name",
            project,
            "start",
            service,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, env=compose_env(token)
        )
        expect(result.returncode == 0, f"{' '.join(command)} failed: {result.stderr.strip()}")
        return f"started {service} again"

    report.run("worker loss: the worker is started again", start_it_again)


def check_profile_isolation(
    fleet: Fleet, other: Client, report: Report, *, resolutions: int
) -> None:
    """A replica that matches on profile places sandboxes only where it fits.

    `SANDBOX_PROFILE_HASH` is what a replica compares before choosing a worker, and
    a fleet that is half rebuilt has two configurations answering the same
    registry. Nothing about the traffic says so: every replica serves, every
    replica registers, and the sandboxes a replica places run on a worker built
    from different settings than the ones it was admitted under.
    """

    def listed_as_live() -> str:
        # Which worker it is comes from what it reports about itself, not from a
        # name in this file: the profile hash is the property under test.
        status, health = other.call("GET", "/healthz")
        expect(status == 200, f"the third replica answered {status}")
        worker_id = str(health["worker"]["id"])
        profile = str(health["worker"]["profile_hash"])

        overview = fleet.clients[0].ok("GET", "/api/v1/admin/overview")
        listed = {str(entry["worker_id"]): entry for entry in overview["workers"]}
        if worker_id not in listed:
            raise Failure(f"{worker_id} is not in the fleet view: {sorted(listed)}")
        expect(bool(listed[worker_id]["live"]), f"{worker_id} is listed but not live")
        expect(
            listed[worker_id]["profile_hash"] == profile,
            f"{worker_id} reports profile {profile} and is listed with "
            f"{listed[worker_id]['profile_hash']}",
        )

        others = {str(entry["profile_hash"]) for key, entry in listed.items() if key != worker_id}
        expect(
            profile not in others,
            f"the third replica runs the same profile as the rest: {profile}",
        )
        return f"{worker_id} is live with profile {profile}, the others with {sorted(others)}"

    report.run("profiles: the odd replica is listed as live", listed_as_live)

    state: dict[str, str] = {}
    state["worker_id"] = str(other.call("GET", "/healthz")[1]["worker"]["id"])

    def never_chosen_by_the_others() -> str:
        chosen: set[str] = set()
        for _ in range(resolutions):
            sandbox = Sandbox(fleet, "fleet-profile")
            sandbox.resolve(fleet.clients[0])
            chosen.add(sandbox.owner)
        expect(
            state.get("worker_id") not in chosen,
            f"a replica with the default profile placed a sandbox on {sorted(chosen)}",
        )
        return f"{resolutions} resolutions, none of them on {state.get('worker_id')}"

    report.run("profiles: the others never place a sandbox on it", never_chosen_by_the_others)

    def it_places_them_on_itself() -> str:
        sandbox = Sandbox(fleet, "fleet-profile-self")
        sandbox.resolve(other)
        expect(
            sandbox.owner == state.get("worker_id"),
            f"a replica with the odd profile placed its own sandbox on {sandbox.owner}, "
            f"and the only worker of its profile is {state.get('worker_id')}",
        )
        return f"its own resolution landed on {sandbox.owner}"

    report.run("profiles: asked directly, it places one on itself", it_places_them_on_itself)


def check_idle_cleanup(fleet: Fleet, report: Report, *, timeout: float) -> None:
    """A sandbox nobody touched is released, and the record says why.

    The idle TTL is the difference between a fleet that reclaims what its tenants
    abandoned and one that fills up with sandboxes nobody is using. It runs on a
    worker with nobody asking it to, so nothing else here would notice it having
    stopped.
    """

    state: dict[str, Any] = {}

    def connect_and_leave() -> str:
        sandbox = Sandbox(fleet, "fleet-idle")
        owner = fleet.clients[0]
        sandbox.resolve(owner)
        sandbox.connect(owner)
        state["sandbox"] = sandbox
        return f"{sandbox.sandbox_id} is READY on {sandbox.owner}, and then left alone"

    report.run("idle cleanup: connect a sandbox and leave it alone", connect_and_leave)
    sandbox = state.get("sandbox")
    if sandbox is None:
        return

    def released_by_itself() -> str:
        owner = fleet.owner_of(sandbox.owner)
        path = f"/api/v1/sandboxes/{sandbox.sandbox_id}"
        deadline = time.time() + timeout
        seen = ""
        while time.time() < deadline:
            _, route = owner.call("GET", path)
            seen = str(route.get("status", route)) if isinstance(route, dict) else str(route)
            if seen == "RELEASED":
                audit = owner.ok("GET", f"{path}/audit")
                expect(
                    audit["last_release_reason"] == "IDLE_TIMEOUT",
                    f"released for {audit['last_release_reason']} instead of being idle",
                )
                expect(
                    audit["last_released_by"] == "system:orphan-reaper",
                    f"released by {audit['last_released_by']}",
                )
                return f"released as IDLE_TIMEOUT by {audit['last_released_by']}"
            time.sleep(2)
        raise Failure(
            f"the sandbox was still {seen} after {timeout:.0f}s with nobody using it; "
            "the fleet needs SANDBOX_IDLE_TTL_SECONDS short for this to be observable in "
            "seconds, and --short-graces to say so"
        )

    report.run("idle cleanup: the worker releases it without being asked", released_by_itself)


def compose_env(token: str) -> dict[str, str]:
    """The fleet file interpolates the token, so Compose needs it too."""

    return {**os.environ, "SANDBOX_INTERNAL_TOKEN": token}


def compose_available(compose_file: str, token: str) -> bool:
    """Whether this fleet is one Compose is running, so a worker can be stopped."""

    try:
        result = subprocess.run(
            ["docker", "compose", "-f", compose_file, "ps", "--quiet"],
            capture_output=True,
            text=True,
            check=False,
            env=compose_env(token),
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=REPLICAS[0], help="the first replica")
    parser.add_argument("--peer-url", default=REPLICAS[1], help="the second replica")
    parser.add_argument(
        "--other-profile-url",
        default=OTHER_PROFILE,
        help="a third replica running with a different SANDBOX_PROFILE_HASH, "
        "started with the file's other-profile profile",
    )
    parser.add_argument(
        "--resolutions",
        type=int,
        default=8,
        help="how many times to resolve before concluding the odd worker is never chosen",
    )
    parser.add_argument("--token", default=TOKEN)
    parser.add_argument("--compose-file", default=COMPOSE_FILE)
    parser.add_argument("--project", default="agent-sandbox-fleet")
    parser.add_argument(
        "--short-graces",
        action="store_true",
        help="the fleet was started with the shortened idle and grace periods the "
        "documentation shows, so the checks that wait for them can run. The periods "
        "are read back from every worker: a fleet running the shipped ones stops the "
        "run, rather than failing the checks that wait for a reaper",
    )
    parser.add_argument(
        "--reclaim-timeout",
        type=float,
        default=120.0,
        help="how long to wait for a reaper with short periods to act",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail when a check could not run, rather than reporting it as skipped",
    )
    options = parser.parse_args()

    if not options.token:
        print("SANDBOX_INTERNAL_TOKEN is not set and --token was not given", file=sys.stderr)
        return 2

    report = Report()
    try:
        fleet = Fleet(
            [Client(options.base_url, options.token), Client(options.peer_url, options.token)]
        )
    except Exception as exc:  # a replica that is not there is not a failed check
        print(
            f"the fleet is not answering: {type(exc).__name__}: {exc} -- "
            f"start it with docker compose -f {options.compose_file} up -d --wait",
            file=sys.stderr,
        )
        return 2

    other = Client(options.other_profile_url, options.token)
    try:
        other.call("GET", "/healthz")
        other_reachable = True
    except Exception:
        other_reachable = False

    if options.short_graces:
        # Before any check: a fleet that was not started with the shortened
        # periods cannot be verified through the checks that wait for a reaper,
        # and finding that out at the end costs the two minutes this flag exists
        # to avoid. Like a fleet that is not answering, it is a setup error, so
        # it stops the run rather than being recorded as a failed check.
        try:
            periods = check_short_graces([*fleet.clients, *([other] if other_reachable else [])])
        except Failure as exc:
            print(
                f"--short-graces asserts the shortened periods: {exc}",
                file=sys.stderr,
            )
            return 2
        report.check("fleet: the replicas run the short periods --short-graces asserts", periods)

    sandboxes: list[Sandbox] = []
    second: Sandbox | None = None
    try:
        check_replicas(fleet, report)

        if other_reachable:
            check_profile_isolation(fleet, other, report, resolutions=options.resolutions)
        else:
            report.skip(
                "profiles",
                f"no replica of another profile at {options.other_profile_url}; start "
                "one with --profile other-profile, as the fleet file shows",
            )

        first = Sandbox(fleet, "fleet-first")
        sandboxes.append(first)
        check_forwarding(fleet, first, report)
        second = check_cross_worker_templates(fleet, first, report)
        if second is not None:
            sandboxes.append(second)

        if options.short_graces:
            check_idle_cleanup(fleet, report, timeout=options.reclaim_timeout)
        else:
            report.skip(
                "idle cleanup",
                "the fleet is running with the shipped grace periods, which are "
                "minutes; start it as documented and pass --short-graces",
            )

        if compose_available(options.compose_file, options.token):
            check_worker_loss(
                fleet,
                first,
                report,
                compose_file=options.compose_file,
                project=options.project,
                token=options.token,
                reclaim_timeout=options.reclaim_timeout,
                sandboxes=sandboxes,
            )
        else:
            report.skip(
                "worker loss",
                f"{options.compose_file} is not running under Compose, so no worker "
                "can be stopped from here",
            )
    finally:
        for sandbox in sandboxes:
            try:
                sandbox.release()
            except Exception as exc:  # cleanup must not mask the result
                # Expected for the sandboxes the worker-loss check orphaned: their
                # owner was stopped, so there is nobody to forward the release to,
                # and that is the case the reaper exists for -- IDLE_TIMEOUT on a
                # live owner, WORKER_LOST on one that stays gone. Said out loud so
                # a run that passed does not look like it left something behind.
                print(
                    f"cleanup: {sandbox.sandbox_id} was not released ({exc}); "
                    "the reaper reclaims it",
                    file=sys.stderr,
                )

    print()
    print(
        f"{report.passed} passed, {report.failed} failed, {report.skipped} skipped",
        flush=True,
    )
    if report.failed or (options.strict and report.skipped):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
