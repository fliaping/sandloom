#!/usr/bin/env python3
"""Verify a running Agent Sandbox the way a client uses it.

    export SANDBOX_INTERNAL_TOKEN=...   # the value the service was started with
    ./scripts/verify-deployment.py
    ./scripts/verify-deployment.py --base-url http://10.0.0.7:8080

Why this exists. The integration suite drives the runtime directly, so it needs
Linux, root, and Bubblewrap on the same host. A deployment is none of those: it
is a container, reached over HTTP. Every defect found by hand in the polyglot
toolchains was invisible to both suites —

* a compiler installed in the image but absent from the sandbox `PATH`;
* `RUSTUP_HOME` pointing at a per-sandbox directory that starts empty;
* a login shell re-reading `/etc/profile`, which resets `PATH` outright;
* no procfs at `basic`, so launchers that resolve `$ORIGIN/../lib` cannot find
  their own shared libraries.

— and all four pass every static check, every unit test, and every integration
test that does not execute a command inside a sandbox. What catches them is
running the languages and reading the output.

So this drives a real service and asserts on what comes back. It creates one or
two sandboxes, releases them at the end, and releases them on failure too.

Exit code is 0 when nothing failed. A check that cannot run is reported as SKIP
with the reason, and a SKIP is not a pass: the summary prints how many were
skipped, and `--strict` turns any skip into a failure.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class Failure(Exception):
    """A check that ran and did not hold."""


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        authenticated: bool = True,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Return (status, decoded body). A 4xx/5xx is returned, not raised."""

        url = f"{self.base_url}{path}"
        if query:
            url = (
                f"{url}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
            )
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise Failure(f"{method} {path}: {exc.reason}") from exc
        text = raw.decode(errors="replace")
        try:
            return status, json.loads(text) if text else None
        except json.JSONDecodeError:
            return status, text


class Report:
    """Collects results so the summary can state what was and was not proven."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def _record(self, outcome: str, name: str, detail: str) -> None:
        self.rows.append((outcome, name, detail))
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[outcome]
        line = f"[{marker}] {name}"
        if detail:
            line = f"{line:60s} {detail}"
        print(line, flush=True)

    def check(self, name: str, detail: str = "") -> None:
        self._record("pass", name, detail)

    def fail(self, name: str, detail: str) -> None:
        self._record("fail", name, detail)

    def skip(self, name: str, detail: str) -> None:
        self._record("skip", name, detail)

    def run(self, name: str, check: Any) -> None:
        """Run one check; a Failure is a FAIL, anything else is a bug."""
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


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)


class Session:
    """One sandbox, with the generation every later call has to carry."""

    def __init__(self, client: Client, sandbox_id: str, scope: str) -> None:
        self.client = client
        self.sandbox_id = sandbox_id
        self.scope = scope
        self.generation = 0
        self.connected = False

    def resolve(self) -> dict[str, Any]:
        status, route = self.client.call(
            "POST",
            "/api/v1/sandboxes/resolve",
            body={
                "sandbox_id": self.sandbox_id,
                "workspace_scope_id": self.scope,
            },
        )
        expect(status == 200, f"resolve answered {status}: {route}")
        self.generation = int(route["generation"])
        return dict(route)

    def connect(self) -> None:
        status, body = self.client.call(
            "POST",
            f"/api/v1/sandboxes/{self.sandbox_id}",
            body={"generation": self.generation},
        )
        expect(status == 200, f"connect answered {status}: {body}")
        self.connected = True

    def start(self) -> dict[str, Any]:
        route = self.resolve()
        self.connect()
        return route

    def release(self) -> None:
        if not self.connected:
            return
        self.client.call(
            "DELETE",
            f"/api/v1/sandboxes/{self.sandbox_id}",
            query={"generation": self.generation},
        )
        self.connected = False

    def exec(
        self,
        argv: list[str],
        *,
        timeout: int = 120,
        scope: str | None = None,
        wait: float = 180.0,
    ) -> dict[str, Any]:
        exec_id = f"verify-{uuid.uuid4().hex[:16]}"
        body: dict[str, Any] = {
            "exec_id": exec_id,
            "generation": self.generation,
            "argv": argv,
            "cwd": "/workspace",
            "timeout_seconds": timeout,
        }
        if scope:
            body["exec_scope"] = scope
        status, response = self.client.call(
            "POST", f"/api/v1/sandboxes/{self.sandbox_id}/exec", body=body
        )
        expect(status == 200, f"exec answered {status}: {response}")
        if response.get("status") in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
            return dict(response)

        deadline = time.time() + wait
        while time.time() < deadline:
            status, result = self.client.call(
                "GET",
                f"/api/v1/sandboxes/{self.sandbox_id}/exec/{exec_id}",
                query={"generation": self.generation},
            )
            expect(status == 200, f"exec status answered {status}: {result}")
            if result.get("status") in {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}:
                return dict(result)
            time.sleep(0.5)
        raise Failure(f"exec did not finish within {wait:.0f}s: {argv[0]}")

    def run(self, script: str, *, login: bool = False, timeout: int = 120) -> dict[str, Any]:
        return self.exec(["/bin/sh", "-lc" if login else "-c", script], timeout=timeout)

    def shell_output(self, script: str, *, login: bool = False) -> str:
        result = self.run(script, login=login)
        return (result.get("stdout") or "") + (result.get("stderr") or "")


# ── the language checks ──
#
# Each one compiles or evaluates real code and prints a marker, so a toolchain
# that is present but unlinked fails here rather than at the user's first build.

LANGUAGE_CHECKS: dict[str, tuple[str, str]] = {
    "python": ("python3", "python3 -c \"print('marker', 6*7)\""),
    "node": ("node", "node -e \"console.log('marker', 6*7)\""),
    "go": (
        "go",
        "mkdir -p /workspace/verify-go && cd /workspace/verify-go && "
        'printf \'package main\\nimport "fmt"\\nfunc main(){fmt.Println("marker", 6*7)}\\n\' > m.go && '
        "(go mod init m >/dev/null 2>&1 || true) && go run -p 2 m.go",
    ),
    "rust": (
        "rustc",
        "cd /workspace && printf 'fn main(){println!(\"marker {}\", 6*7);}\\n' > m.rs && "
        "rustc m.rs -o /tmp/verify-rs && /tmp/verify-rs",
    ),
    "java": (
        "javac",
        "cd /workspace && printf '%s\\n' "
        "'public class V{public static void main(String[] a){System.out.println(\"marker \"+(6*7));}}' "
        "> V.java && javac V.java && java V",
    ),
    "typescript": (
        "tsc",
        "mkdir -p /workspace/verify-ts && cd /workspace/verify-ts && "
        "printf 'const value: number = 6 * 7; console.log(\"marker\", value);\\n' > m.ts && "
        "tsc m.ts --target ES2020 --module commonjs --outDir out && node out/m.js",
    ),
}

LANGUAGE_ORDER = ("python", "node", "go", "rust", "java")


# ── MCP ──
#
# The 2026-07-28 revision of Streamable HTTP carries the handshake in a `_meta`
# envelope on every request rather than in an `initialize` call, so a stateless
# server needs no session and answers `initialize` with "Method not found". The
# transport also requires the method — and, for a call, the tool name — as a
# header; the official SDK sets those for you, so a client written by hand is
# the only place they show up.
MCP_PATH = "/api/v1/sandboxes/mcp/streamable-http"
MCP_PROTOCOL = "2026-07-28"
MCP_ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL,
    "io.modelcontextprotocol/clientCapabilities": {},
}

# The toolset the MCP server publishes. Pinned here because it is a public
# contract: an agent is written against these names, and a tool that quietly
# disappears breaks it.
MCP_TOOLS = {
    "sandbox_profile",
    "sandbox_resolve",
    "sandbox_create",
    "sandbox_status",
    "sandbox_audit_get",
    "sandbox_exec",
    "sandbox_exec_status",
    "sandbox_cancel",
    "sandbox_write_file",
    "sandbox_read_file",
    "sandbox_release",
}


def mcp_request(
    client: Client, method: str, params: dict[str, Any], *, name: str = ""
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL,
        "mcp-method": method,
    }
    if name:
        headers["mcp-name"] = name
    return client.call(
        "POST",
        MCP_PATH,
        body={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        extra_headers=headers,
    )


def mcp_call(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    status, body = mcp_request(
        client,
        "tools/call",
        {"name": tool, "arguments": arguments, "_meta": MCP_ENVELOPE},
        name=tool,
    )
    expect(status == 200, f"tools/call {tool} answered {status}: {body}")
    expect("error" not in body, f"tools/call {tool} failed: {body}")
    result = body["result"]
    expect(result.get("isError") is not True, f"{tool} reported an error: {result}")
    # `structuredContent` is what a program reads and `content` is the prose a
    # model reads. Both are contract, so both have to be there.
    expect(
        isinstance(result.get("structuredContent"), dict),
        f"{tool} returned no structured content: {result}",
    )
    expect(result.get("content"), f"{tool} returned no content blocks: {result}")
    return dict(result["structuredContent"])


def check_mcp(client: Client, report: Report) -> None:
    """The MCP surface, which is the documented way an agent drives this."""

    status, body = mcp_request(client, "tools/list", {"_meta": MCP_ENVELOPE})
    expect(status == 200, f"tools/list answered {status}: {body}")
    expect("error" not in body, f"tools/list failed: {body}")
    names = {tool["name"] for tool in body["result"]["tools"]}
    expect(
        names == MCP_TOOLS,
        f"the MCP toolset changed: missing {sorted(MCP_TOOLS - names)}, "
        f"unexpected {sorted(names - MCP_TOOLS)}",
    )
    report.check("mcp: the toolset is published", f"{len(names)} tools")

    profile = mcp_call(client, "sandbox_profile", {})
    expect(
        profile.get("transport") == "streamable-http" and profile.get("stateless") is True,
        f"the MCP transport is not the documented one: {profile}",
    )
    expect(profile.get("healthy") is True, f"the worker reports itself unhealthy: {profile}")
    report.check("mcp: sandbox_profile describes the worker", str(profile.get("worker_id")))

    # The point of the surface: an agent can operate a sandbox through it. Run
    # the same lifecycle the REST checks run, over MCP instead.
    sandbox_id = f"verify-mcp-{uuid.uuid4().hex[:8]}"
    route = mcp_call(
        client, "sandbox_resolve", {"sandbox_id": sandbox_id, "workspace_scope_id": sandbox_id}
    )
    generation = int(route["generation"])
    created = mcp_call(
        client, "sandbox_create", {"sandbox_id": sandbox_id, "generation": generation}
    )
    expect(created.get("status") in {"READY", "ASSIGNED"}, f"create said {created}")
    try:
        executed = mcp_call(
            client,
            "sandbox_exec",
            {
                "sandbox_id": sandbox_id,
                "generation": generation,
                "exec_id": f"mcp-{uuid.uuid4().hex[:8]}",
                "argv": ["/bin/sh", "-c", "echo mcp-marker"],
            },
        )
        expect(
            executed.get("status") == "SUCCEEDED"
            and "mcp-marker" in (executed.get("stdout") or ""),
            f"exec through MCP did not run: {executed}",
        )
        report.check("mcp: an agent can execute a command", "exit 0")
    finally:
        released = mcp_call(client, "sandbox_release", {"sandbox_id": sandbox_id})
        expect(released.get("status") == "RELEASED", f"release said {released}")
    report.check("mcp: an agent can release the sandbox")

    status, _ = client.call(
        "POST",
        MCP_PATH,
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        authenticated=False,
        extra_headers={"Accept": "application/json, text/event-stream"},
    )
    expect(status == 401, f"the MCP endpoint answered {status} without a token")
    report.check("mcp: the endpoint requires the token", "401 without it")


def check_health(client: Client, report: Report) -> dict[str, Any]:
    status, body = client.call("GET", "/health", authenticated=False)
    expect(status == 200 and body == "ok", f"/health answered {status}: {body!r}")
    report.check("health: /health is ok")

    status, body = client.call("GET", "/healthz", authenticated=False)
    expect(status == 200, f"/healthz answered {status}: {body}")
    expect(isinstance(body, dict), f"/healthz did not return JSON: {body!r}")

    capabilities = body["worker"]["capabilities"]
    expect(
        capabilities.get("selected_level") in {"basic", "standard", "strict"},
        f"unsupported isolation level: {capabilities.get('selected_level')!r}",
    )
    report.check(
        "health: /healthz reports the negotiated level",
        f"{capabilities['selected_level']} (profile {body['worker']['profile_hash']})",
    )

    expect(
        isinstance(capabilities.get("probe_failures"), dict),
        "/healthz does not report probe_failures, so a downgrade is unexplainable",
    )
    failures = capabilities["probe_failures"]
    report.check(
        "health: a downgrade explains itself",
        "no downgrade" if not failures else f"higher levels refused: {', '.join(failures)}",
    )

    toolchains = capabilities.get("toolchains")
    expect(
        isinstance(toolchains, dict) and "configured" in toolchains,
        "/healthz does not publish the configured toolchains",
    )
    report.check(
        "health: /healthz publishes the toolchains",
        f"configured {', '.join(toolchains['configured'])}",
    )
    return dict(capabilities)


def check_lifecycle(client: Client, report: Report, session: Session) -> None:
    route = session.resolve()
    report.check(
        "lifecycle: resolve assigns a worker",
        f"worker {route['worker_id']} generation {route['generation']} uid {route['sandbox_uid']}",
    )
    expect(
        int(route["generation"]) >= 1 and int(route["sandbox_uid"]) > 0,
        f"resolve returned an unusable route: {route}",
    )

    session.connect()
    report.check("lifecycle: connect prepares the sandbox", "ready")

    result = session.run('echo "hello from $(hostname)"')
    expect(result["exit_code"] == 0, f"a trivial command failed: {result}")
    expect("hello from" in result["stdout"], f"unexpected stdout: {result['stdout']!r}")
    report.check("lifecycle: execute a command", f"exit {result['exit_code']}")

    # The fencing token is the property that keeps a reassigned workspace from
    # being written by the previous owner, so it is checked rather than assumed.
    status, body = client.call(
        "POST",
        f"/api/v1/sandboxes/{session.sandbox_id}/exec",
        body={
            "exec_id": f"stale-{uuid.uuid4().hex[:8]}",
            "generation": session.generation + 1,
            "argv": ["/bin/true"],
        },
    )
    expect(
        status == 409 and "STALE" in json.dumps(body),
        f"a stale generation was not refused: {status} {body}",
    )
    report.check("lifecycle: a stale generation is refused", "409")

    # A failed command is reported as a failure, not as an error of the API.
    result = session.run("exit 3")
    expect(result["exit_code"] == 3, f"exit code was not propagated: {result}")
    report.check("lifecycle: a non-zero exit code is propagated", "exit 3")

    # Output is bounded, so a runaway command cannot exhaust the worker.
    result = session.run("head -c 20000000 /dev/zero | tr '\\0' 'x'", timeout=120)
    expect(
        result.get("truncated") is True or len(result.get("stdout", "")) < 20000000,
        "an oversized output was not bounded",
    )
    report.check("lifecycle: oversized output is bounded", f"truncated={result.get('truncated')}")

    # A documented field that the deployment cannot honour has to say so. With
    # no credential broker configured the service refuses `sensitive_env`; with
    # one it may accept it. Either answer is fine, and this check is written to
    # accept both — what it will not accept is a 5xx, which is what shipped: the
    # broker was read off the execution backend, which has no such attribute, so
    # any request carrying the field died with an AttributeError and the client
    # saw only "Internal Server Error".
    status, body = client.call(
        "POST",
        f"/api/v1/sandboxes/{session.sandbox_id}/exec",
        body={
            "exec_id": f"verify-{uuid.uuid4().hex[:16]}",
            "generation": session.generation,
            "argv": ["true"],
            "sensitive_env": {"SANDBOX_VERIFY_PROBE": "not-a-real-secret"},
        },
    )
    expect(
        status < 500,
        f"a request carrying sensitive_env answered {status}: {body}",
    )
    report.check(
        "lifecycle: an unsupported field is refused with a reason",
        f"{status} {str(body)[:60]}",
    )


def check_files(client: Client, report: Report, session: Session) -> None:
    generation = session.generation
    sandbox = session.sandbox_id

    def call(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        status, response = client.call(method, path, body=body)
        expect(status == 200, f"{method} {path} answered {status}: {response}")
        return response

    call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/files/mkdir",
        {"generation": generation, "path": "/workspace/verify/notes", "parents": True},
    )
    payload = base64.b64encode(b"the quick brown fox\n").decode()
    call(
        "PUT",
        f"/api/v1/sandboxes/{sandbox}/files",
        {
            "generation": generation,
            "path": "/workspace/verify/notes/a.txt",
            "content_base64": payload,
        },
    )

    # Named `route` rather than `path`: the query itself carries a `path`
    # parameter, and a positional of the same name would swallow it.
    def get(route: str, **query: Any) -> Any:
        status, response = client.call("GET", route, query=query)
        expect(status == 200, f"GET {route} answered {status}: {response}")
        return response

    read = get(
        f"/api/v1/sandboxes/{sandbox}/files",
        path="/workspace/verify/notes/a.txt",
        generation=generation,
    )
    expect(
        base64.b64decode(read["content_base64"]) == b"the quick brown fox\n",
        "the file did not read back byte for byte",
    )

    listing = get(
        f"/api/v1/sandboxes/{sandbox}/files/list",
        path="/workspace/verify/notes",
        generation=generation,
    )
    names = {entry["name"] for entry in listing["entries"]}
    expect(names == {"a.txt"}, f"the listing did not match what was written: {names}")
    expect(
        next(e for e in listing["entries"] if e["name"] == "a.txt")["type"] == "file",
        "a regular file was not reported as one",
    )
    report.check("files: write, read back, and list", "one file, byte-for-byte")

    # A write through the API and a read through the sandbox must agree: the
    # file API and the execution backend have to be looking at one tree.
    result = session.run("cat /workspace/verify/notes/a.txt")
    expect(
        result["stdout"] == "the quick brown fox\n",
        f"the sandbox did not see what the file API wrote: {result['stdout']!r}",
    )
    report.check("files: the sandbox sees what the file API wrote")

    # And the other direction, which is how a caller collects what a command
    # produced: the artifact is written inside the sandbox and fetched through
    # the API. One direction working without the other is a client that can
    # start work and never retrieve its results. Written beside the directory
    # the checks below list and move inside of, so they still see exactly what
    # each of them put there.
    session.run("printf 'from the sandbox\\n' > /workspace/verify/collected.txt")
    collected = get(
        f"/api/v1/sandboxes/{sandbox}/files",
        path="/workspace/verify/collected.txt",
        generation=generation,
    )
    expect(
        base64.b64decode(collected["content_base64"]) == b"from the sandbox\n",
        "the API did not read what the sandbox wrote",
    )
    report.check("files: the API reads what the sandbox wrote")

    call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/files/move",
        {
            "generation": generation,
            "source": "/workspace/verify/notes/a.txt",
            "destination": "/workspace/verify/notes/b.txt",
        },
    )
    listing = get(
        f"/api/v1/sandboxes/{sandbox}/files/list",
        path="/workspace/verify/notes",
        generation=generation,
    )
    expect(
        {entry["name"] for entry in listing["entries"]} == {"b.txt"},
        "the move did not rename the entry",
    )
    report.check("files: move")

    # Traversal has to be refused by the API, and the refusal has to be a client
    # error rather than a 5xx.
    status, body = client.call(
        "GET",
        f"/api/v1/sandboxes/{sandbox}/files",
        query={"path": "/workspace/../../etc/passwd", "generation": generation},
    )
    expect(
        400 <= status < 500,
        f"a traversal was not refused as a client error: {status} {body}",
    )
    report.check("files: a path traversal is refused", f"{status}")

    # A populated directory needs `recursive`, so a mistyped path cannot take a
    # tree with it.
    status, body = client.call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/files/delete",
        body={"generation": generation, "path": "/workspace/verify"},
    )
    expect(status != 200, f"a populated directory was deleted without recursive: {body}")
    report.check("files: a populated delete needs to be asked for", f"{status}")

    call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/files/delete",
        {"generation": generation, "path": "/workspace/verify", "recursive": True},
    )
    report.check("files: recursive delete")


def check_toolchains(report: Report, session: Session, capabilities: dict[str, Any]) -> list[str]:
    """Compile and run a program in every language the worker was given."""

    toolchains = capabilities.get("toolchains") or {}
    configured = list(toolchains.get("configured") or LANGUAGE_ORDER)
    unavailable = set(toolchains.get("unavailable_at_this_level") or ())
    checks = toolchains.get("checks") or {}
    level = capabilities.get("selected_level")
    worked: list[str] = []

    names = [n for n in LANGUAGE_ORDER if n in configured]
    if "node" in configured and checks.get("typescript", {}).get("status") == "available":
        names.append("typescript")
    for name in names:
        binary, script = LANGUAGE_CHECKS[name]
        present = session.run(f"command -v {binary} >/dev/null 2>&1 && echo yes || echo no")
        if "yes" not in present["stdout"]:
            report.skip(
                f"toolchain: {name}",
                f"{binary} is not in this image — enable it in SANDBOX_TOOLCHAINS "
                f"and build the image that ships it",
            )
            continue

        measured = checks.get(name)
        if measured and measured.get("status") in {"failed", "timed_out"}:
            report.skip(
                f"toolchain: {name}",
                f"sandbox launch probe {measured['status']} at {level!r}: "
                f"{measured.get('detail', '')}",
            )
            continue
        if name in unavailable and not measured:
            report.skip(
                f"toolchain: {name}",
                f"{binary} is present but isolation level {level!r} provides no private "
                f"procfs, so it cannot locate its own libraries; see docs/TOOLCHAINS.md",
            )
            continue

        # Both forms, because `bash -lc` is how most agents run a command and a
        # login shell re-reads /etc/profile.
        for login in (False, True):
            form = "login shell" if login else "plain shell"
            result = session.run(script, login=login, timeout=300)
            output = (result["stdout"] or "") + (result["stderr"] or "")
            expect(
                result["exit_code"] == 0 and "marker 42" in output,
                f"{form}: exit {result['exit_code']}, output {output.strip()[:300]!r}",
            )
        report.check(f"toolchain: {name}", f"{binary} compiles and runs, login and plain")

        # The two forms must agree on PATH. This is the defect that made every
        # language disappear for a login shell while everything else looked fine.
        plain = session.run("echo $PATH")["stdout"].strip()
        login = session.run("echo $PATH", login=True)["stdout"].strip()
        expect(
            plain == login,
            f"PATH differs between the two shell forms:\n  plain: {plain}\n  login: {login}",
        )
        worked.append(name)

    if worked:
        report.check("toolchain: both shell forms agree on PATH", ", ".join(worked))
    return worked


def _template_scope(client: Client) -> tuple[bool, int]:
    """Whether a template can cross workers here, and how many workers there are.

    `GET /api/v1/templates` is answered by the node that received it, so it
    reports that worker's catalog. Without an object store a template exists
    only on the worker that built it, and this script cannot choose where the
    sandbox under test lands — so on a multi-worker deployment with no store,
    the sandbox may be on a different node than the one being asked. That is a
    supported configuration, not a fault, and the build-and-mount checks below
    cannot be read meaningfully there.

    The overview is the deployment's own answer to this, which is better than
    guessing. If it cannot answer, assume the strict single-catalog case and let
    the checks report what they find.
    """

    status, overview = client.call("GET", "/api/v1/admin/overview")
    if status != 200 or not isinstance(overview, dict):
        return (True, 1)
    return (bool(overview.get("templates_shared")), int(overview.get("worker_total") or 1))


def check_templates(client: Client, report: Report, session: Session) -> None:
    """Build a template, mount it somewhere else, and take it out of service."""

    shared, workers = _template_scope(client)
    if not shared and workers > 1:
        report.skip(
            "templates: cross-worker sharing",
            f"{workers} workers and no object store, so a template is readable only on "
            "the worker that built it; set SANDBOX_BLOBSTORE_* to verify sharing "
            "(docs/TEMPLATES.md)",
        )
        return

    name = f"verify-{uuid.uuid4().hex[:8]}"
    generation = session.generation
    sandbox = session.sandbox_id

    session.run(
        f"mkdir -p /envs/{name}/bin && printf 'hello from the template\\n' > /envs/{name}/bin/hello.txt"
    )

    status, built = client.call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/templates",
        body={
            "generation": generation,
            "name": name,
            "source_path": f"/envs/{name}",
            "description": "created by scripts/verify-deployment.py",
        },
    )
    expect(status == 200, f"building a template answered {status}: {built}")
    # A template mounts at /envs/<name>, so a mismatch between the name and the
    # source directory leaves the binaries pointing where they were built.
    expect(
        built["mount_target"] == f"/envs/{name}",
        f"the mount target was not derived from the name: {built['mount_target']}",
    )
    expect(built["size_bytes"] > 0, f"the template is empty: {built}")
    report.check(
        "templates: build from a sandbox directory",
        f"{built['size_bytes']} bytes, digest {built['digest'][:19]}…",
    )

    status, listing = client.call("GET", "/api/v1/templates")
    expect(status == 200, f"listing templates answered {status}: {listing}")
    expect(
        any(entry["name"] == name for entry in listing["templates"]),
        f"the new template is not in the catalog: {listing}",
    )
    report.check("templates: the catalog lists it")

    # The point of the feature: a second sandbox gets the tree without the work.
    consumer = Session(client, f"verify-{uuid.uuid4().hex[:8]}", f"verify/{uuid.uuid4().hex[:8]}")
    try:
        consumer.start()
        status, attached = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [name]},
        )
        expect(status == 200, f"attaching a template answered {status}: {attached}")
        result = consumer.run(f"cat /envs/{name}/bin/hello.txt")
        expect(
            result["exit_code"] == 0 and "hello from the template" in result["stdout"],
            f"the mounted template was not readable: {result}",
        )
        report.check("templates: a second sandbox mounts it read-only", f"/envs/{name}")

        # Read-only is the property that makes sharing one tree across tenants
        # safe, so it is checked rather than assumed.
        result = consumer.run(f"touch /envs/{name}/bin/denied 2>&1; echo rc=$?")
        expect(
            "rc=0" not in result["stdout"],
            f"a sandbox wrote into a mounted template: {result['stdout']!r}",
        )
        report.check("templates: the mount is read-only")

        # More than one environment at once is the normal case for an
        # application -- a Python env beside a Node tree, or two Python
        # versions -- and each mounts at /envs/<name>, so this is several bind
        # mounts into one sandbox. A mount list that stopped after the first
        # entry would look exactly like an environment that was never published.
        second_name = f"{name}-second"
        session.run(
            f"mkdir -p /envs/{second_name}/bin && "
            f"printf 'hello from the second template\\n' > /envs/{second_name}/bin/hello.txt",
            login=True,
        )
        status, second = client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox}/templates",
            body={
                "generation": generation,
                "name": second_name,
                "source_path": f"/envs/{second_name}",
                "description": "created by scripts/verify-deployment.py",
            },
        )
        expect(status == 200, f"building a second template answered {status}: {second}")
        status, both = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [name, second_name]},
        )
        expect(status == 200, f"attaching two templates answered {status}: {both}")
        result = consumer.run(
            f"cat /envs/{name}/bin/hello.txt && cat /envs/{second_name}/bin/hello.txt"
        )
        expect(
            result["exit_code"] == 0
            and "hello from the template" in result["stdout"]
            and "hello from the second template" in result["stdout"],
            f"two environments did not both mount: {result}",
        )
        report.check(
            "templates: two environments mount side by side",
            f"/envs/{name} and /envs/{second_name}",
        )

        # The reason templates exist is a second environment, and the second
        # environment an application asks for is usually another interpreter
        # version. The image carries two: the platform Python this project
        # provides (3.13, first on PATH) and Debian's own (3.11, at
        # /usr/bin). Building a venv from the second one is the recipe
        # docs/TEMPLATES.md gives, so it is run here rather than described:
        # `uv` is in the image and needs no network to make a venv from an
        # interpreter that is already on disk.
        other_python = "/usr/bin/python3.11"
        python_name = f"{name}-py311"
        session.run(
            f"uv venv --python {other_python} /envs/{python_name}",
            login=True,
        )
        status, published = client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox}/templates",
            body={
                "generation": generation,
                "name": python_name,
                "source_path": f"/envs/{python_name}",
                "description": "created by scripts/verify-deployment.py",
            },
        )
        expect(status == 200, f"publishing the other interpreter answered {status}: {published}")
        status, attached = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [python_name]},
        )
        expect(status == 200, f"attaching it answered {status}: {attached}")
        result = consumer.run(
            f"/envs/{python_name}/bin/python -c "
            "'import sys; print(sys.version_info[0], sys.version_info[1])'"
        )
        expect(result["exit_code"] == 0, f"the mounted interpreter did not run: {result}")
        expect(
            result["stdout"].strip() == "3 11",
            f"the second environment reports {result['stdout'].strip()!r}, not the "
            f"interpreter it was built from ({other_python})",
        )
        report.check(
            "templates: an environment from another interpreter version runs",
            f"{other_python} -> {result['stdout'].strip()}, mounted on the other sandbox",
        )

        # A pinned digest is reproducible; a bare name tracks the catalog.
        digest = built["digest"]
        status, pinned = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [f"{name}@{digest}"]},
        )
        expect(status == 200, f"attaching a pinned digest answered {status}: {pinned}")
        report.check("templates: a pinned digest attaches", f"{digest[:19]}…")

        # The list is a set. A caller that unions several capability sets repeats
        # a template, and refusing that turns a correct request into an error.
        status, repeated = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [name, name]},
        )
        expect(status == 200, f"listing one template twice answered {status}: {repeated}")
        mounted = [entry["name"] for entry in repeated["templates"]]
        expect(mounted == [name], f"a repeated template was not treated as a set: {mounted}")
        report.check("templates: the same revision listed twice mounts once")

        # A second revision of the same name is a different environment, and both
        # would mount at /envs/<name>. That has to be refused, and the message has
        # to name the two revisions — "demo and demo" identifies nothing.
        session.run(f"printf 'a second revision\\n' >> /envs/{name}/bin/hello.txt")
        status, revised = client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox}/templates",
            body={
                "generation": generation,
                "name": name,
                "source_path": f"/envs/{name}",
                "description": "created by scripts/verify-deployment.py",
            },
        )
        expect(status == 200, f"publishing a second revision answered {status}: {revised}")
        expect(
            revised["digest"] != digest,
            f"a changed tree kept its digest, so the digest is not the content: {revised}",
        )
        report.check("templates: a changed tree is a new revision", f"{revised['digest'][:19]}…")

        status, refused = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={
                "generation": consumer.generation,
                "templates": [f"{name}@{digest}", f"{name}@{revised['digest']}"],
            },
        )
        expect(
            400 <= status < 500,
            f"two revisions at one mount target were not refused: {status} {refused}",
        )
        said = refused if isinstance(refused, str) else json.dumps(refused)
        expect(
            digest in said and revised["digest"] in said,
            f"the refusal does not name both revisions: {refused}",
        )
        # The refused call must not have disturbed what was already attached.
        result = consumer.run(f"cat /envs/{name}/bin/hello.txt")
        expect(
            result["exit_code"] == 0 and "second revision" not in result["stdout"],
            f"a refused attach disturbed the existing mount: {result}",
        )
        report.check("templates: two revisions of one name are refused, by revision")

        # Unpublishing removes a name, not the bytes. A sandbox that already has
        # the revision attached is mounting a tree the catalog no longer needs to
        # know about, so taking the name out of service must not reach into a
        # running sandbox -- and it must stop anyone *starting* to use it, which
        # is the half an operator is relying on when they retire an environment.
        status, body = client.call("DELETE", f"/api/v1/templates/{name}")
        expect(status == 200, f"unpublishing answered {status}: {body}")
        status, listing = client.call("GET", "/api/v1/templates")
        expect(
            not any(entry["name"] == name for entry in listing["templates"]),
            "the template survived being deleted",
        )
        report.check("templates: delete removes it from the catalog")

        result = consumer.run(f"cat /envs/{name}/bin/hello.txt")
        expect(
            result["exit_code"] == 0,
            f"unpublishing stopped a sandbox that had already mounted it: {result}",
        )
        report.check(
            "templates: a sandbox that already mounted it keeps running",
            "the mount outlives the name that was retired",
        )

        status, refused = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [name]},
        )
        expect(
            400 <= status < 500,
            f"an unpublished name was attached again: {status} {refused}",
        )
        report.check("templates: a new attach by name is refused")

        # A pin names a revision, but resolution starts from the name, so a pin
        # does not resurrect a name the catalog no longer has. That is the
        # documented rule, and the reason DELETE is not a private hold on a
        # revision: retiring an environment really does take it out of service.
        status, refused = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={
                "generation": consumer.generation,
                "templates": [f"{name}@{revised['digest']}"],
            },
        )
        expect(
            400 <= status < 500,
            f"a pinned digest attached a name the catalog no longer has: {status} {refused}",
        )
        report.check("templates: a pinned digest does not resurrect an unpublished name")

        # And the refusal is about the name, not the content. The tree is still
        # in the builder sandbox, so publishing it again yields the same
        # revision and a new sandbox can attach it: identical content has to
        # produce an identical digest, or a rollback would be a rebuild.
        status, republished = client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox}/templates",
            body={
                "generation": generation,
                "name": name,
                "source_path": f"/envs/{name}",
                "description": "republished by scripts/verify-deployment.py",
            },
        )
        expect(status == 200, f"republishing answered {status}: {republished}")
        expect(
            republished["digest"] == revised["digest"],
            f"an unchanged tree became a new revision: {republished['digest']} is not "
            f"{revised['digest']}",
        )
        report.check("templates: republishing the same tree is the same revision")

        status, restored = client.call(
            "PUT",
            f"/api/v1/sandboxes/{consumer.sandbox_id}/templates",
            body={"generation": consumer.generation, "templates": [name]},
        )
        expect(status == 200, f"attaching the restored name answered {status}: {restored}")
        result = consumer.run(f"cat /envs/{name}/bin/hello.txt")
        expect(
            result["exit_code"] == 0 and "second revision" in result["stdout"],
            f"the restored name did not mount the revision it points at: {result}",
        )
        report.check("templates: the restored name attaches what it points at")
    finally:
        consumer.release()
        # Best effort, so a failure above does not leave a name published for
        # the rest of the run: deleting a name that is already gone is a no-op
        # from here.
        for retired in (name, f"{name}-second", f"{name}-py311"):
            try:
                client.call("DELETE", f"/api/v1/templates/{retired}")
            except Exception:
                pass


def check_admin(client: Client, report: Report) -> None:
    """The console's API, which is what an operator actually reads."""

    for path in ("/api/v1/admin/overview", "/api/v1/admin/sandboxes", "/api/v1/admin/execs"):
        status, body = client.call("GET", path)
        expect(status == 200, f"{path} answered {status}: {body}")

    status, console = client.call("GET", "/admin", authenticated=False)
    expect(status == 200, f"/admin answered {status}")
    expect("<!doctype html" in console.lower(), "/admin did not return the console page")
    # No CDN and no build step: the page has to be self-contained.
    expect(
        'src="http' not in console.replace("http://www.w3.org", ""),
        "/admin references an external script, so it needs a network to render",
    )
    report.check("admin: overview, sandboxes, execs, and the page itself")

    status, body = client.call("GET", "/api/v1/admin/sandboxes", query={"limit": 1})
    expect(status == 200 and "sandboxes" in body, f"paged listing answered {status}: {body}")


def check_authentication(client: Client, report: Report) -> None:
    """A deployment that answers without a token is not a deployment."""

    for path in ("/api/v1/templates", "/api/v1/admin/overview"):
        status, _ = client.call("GET", path, authenticated=False)
        expect(status == 401, f"{path} answered {status} without a token")
    status, _ = client.call("GET", "/api/v1/templates", authenticated=False)
    report.check("auth: the API requires the internal token", "401 without it")


# The address the README's first command reaches without being told, and the
# one this script defaults to as well.
DOCUMENTED_BASE_URL = "http://127.0.0.1:8080"


def check_examples(client: Client, report: Report) -> None:
    """Run the scripts the README tells a reader to run.

    An example is documentation that has to execute. Nothing else in the project
    runs these, so without this they are free to rot into a script that is
    wrong in exactly the way a reader cannot debug.
    """

    directory = Path(__file__).resolve().parents[1] / "examples"
    for script in ("quickstart.py", "templates.py"):
        path = directory / script
        expect(path.exists(), f"{path} is missing; the README points at it")
        environment = {**os.environ, "SANDBOX_INTERNAL_TOKEN": client.token}
        # The README's command carries no --base-url, and every other run passes
        # one, so a default that drifted would fail for every reader while this
        # stayed green. When the deployment is the one the README describes, one
        # of the two is run the way the reader runs it.
        argv = [sys.executable, str(path)]
        if not (script == "quickstart.py" and client.base_url.rstrip("/") == DOCUMENTED_BASE_URL):
            argv += ["--base-url", client.base_url]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=900,
                env=environment,
            )
        except subprocess.TimeoutExpired:
            raise Failure(f"examples/{script} did not finish in 900s") from None
        tail = (completed.stdout + completed.stderr).strip().splitlines()[-6:]
        expect(
            completed.returncode == 0,
            f"examples/{script} exited {completed.returncode}: " + " | ".join(tail),
        )
        report.check(
            f"examples: {script} runs against this deployment",
            "as the README invokes it" if "--base-url" not in argv else client.base_url,
        )


def check_release(client: Client, report: Report, session: Session) -> None:
    sandbox = session.sandbox_id
    generation = session.generation

    # One command whose output is known, so the audit trail can be checked
    # against it once the sandbox it ran in is gone.
    marker = f"recorded-{uuid.uuid4().hex[:8]}"
    result = session.run(f"printf '%s\\n' '{marker}'")
    expect(result["exit_code"] == 0, f"the command to be recorded failed: {result}")

    status, body = client.call(
        "DELETE", f"/api/v1/sandboxes/{sandbox}", query={"generation": generation}
    )
    expect(status == 200, f"release answered {status}: {body}")
    session.connected = False

    status, body = client.call(
        "POST",
        f"/api/v1/sandboxes/{sandbox}/exec",
        body={
            "exec_id": f"after-{uuid.uuid4().hex[:8]}",
            "generation": generation,
            "argv": ["/bin/true"],
        },
    )
    expect(
        400 <= status < 500,
        f"a command after release was not refused as a client error: {status} {body}",
    )
    report.check("release: the sandbox is gone and commands are refused", f"{status}")

    # An audit trail is read after the fact, and the sandbox API cannot serve it:
    # `GET /api/v1/sandboxes/{id}/exec/{exec_id}` validates the live route and
    # answers 409 for a released sandbox, which is the state every sandbox
    # reaches. The admin route reads the record, and the record has the output.
    status, listing = client.call(
        "GET", "/api/v1/admin/execs", query={"sandbox_id": sandbox, "limit": 50}
    )
    expect(status == 200, f"the execution listing answered {status}: {listing}")
    expected = [row for row in listing["execs"] if marker in " ".join(row.get("argv") or [])]
    expect(bool(expected), f"no recorded execution carries {marker}: {listing['execs'][:2]}")
    entry = expected[0]
    status, detail = client.call(
        "GET", f"/api/v1/admin/execs/{entry['sandbox_id']}/{entry['exec_id']}"
    )
    expect(status == 200, f"reading a recorded execution answered {status}: {detail}")
    expect(
        marker in detail["stdout"],
        f"the recorded execution came back without the output it produced: {detail}",
    )
    report.check(
        "release: what the sandbox executed is still readable",
        f"{detail['exec_id']} read after its sandbox was released",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SANDBOX_BASE_URL", "http://127.0.0.1:8080"),
        help="where the service is listening (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SANDBOX_INTERNAL_TOKEN"),
        help="the internal token the service was started with (default: $SANDBOX_INTERNAL_TOKEN)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat a skipped check as a failure, so a partial verification cannot pass",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the sandbox and template behind instead of releasing them",
    )
    args = parser.parse_args()

    if not args.token:
        print(
            "Set SANDBOX_INTERNAL_TOKEN (or pass --token). It is the value the "
            "service was started with.",
            file=sys.stderr,
        )
        return 2

    client = Client(args.base_url, args.token)
    report = Report()
    print(f"verifying {client.base_url}\n")

    sandbox_id = f"verify-{uuid.uuid4().hex[:12]}"
    session = Session(client, sandbox_id, f"verify/{sandbox_id}")
    capabilities: dict[str, Any] = {}

    try:
        capabilities = check_health(client, report)
        check_authentication(client, report)
        check_lifecycle(client, report, session)
        check_files(client, report, session)
        check_toolchains(report, session, capabilities)
        check_templates(client, report, session)
        check_examples(client, report)
        check_mcp(client, report)
        check_admin(client, report)
        check_release(client, report, session)
    except Failure as exc:
        report.fail("verification halted", str(exc))
    except Exception as exc:
        report.fail("verification halted", f"{type(exc).__name__}: {exc}")
    finally:
        if not args.keep:
            try:
                session.release()
            except Exception:
                pass

    print(f"\n{report.passed} passed, {report.failed} failed, {report.skipped} skipped")
    if report.skipped:
        print("A skipped check is not a pass: read the reason next to it.")
    if report.failed:
        return 1
    if args.strict and report.skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
