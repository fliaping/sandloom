#!/usr/bin/env python3
"""End-to-end use of a running Agent Sandbox, with no third-party imports.

Run it against a service you already started (see the README quick start):

    export SANDBOX_INTERNAL_TOKEN=...      # the value the service was started with
    python examples/quickstart.py
    python examples/quickstart.py --base-url http://10.0.0.7:8080

It walks the whole lifecycle once — resolve, connect, execute, write, list,
move, delete, release — and prints what it did at each step. Read it top to
bottom; every call it makes is a call your own client will make.

The one rule worth internalizing: `generation` is a fencing token. Every
request after `resolve` carries the generation that `resolve` returned, and
the service rejects a request that carries an old one. Keep it alongside the
sandbox id and you cannot accidentally act on a reassigned workspace.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

# The same shape the service enforces on a sandbox id. Checking it here means a
# typo fails with a sentence instead of an InvalidURL traceback from deep
# inside urllib.
_SANDBOX_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")


class SandboxClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def call(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            encoded = urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            url = f"{url}?{encoded}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            # The service answers failures with a stable code rather than a
            # sentence, so a client can branch on it. Print it as-is.
            raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        return json.loads(payload) if payload else None


def step(message: str) -> None:
    print(f"  {message}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
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
        "--sandbox-id",
        default=f"quickstart-{uuid.uuid4().hex[:8]}",
        help="stable id for this sandbox (default: a random one)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the sandbox running instead of releasing it at the end",
    )
    args = parser.parse_args()

    if not args.token:
        print(
            "Set SANDBOX_INTERNAL_TOKEN (or pass --token). It is the value the "
            "service was started with.",
            file=sys.stderr,
        )
        return 2

    client = SandboxClient(args.base_url, args.token)
    sandbox_id = args.sandbox_id
    scope = f"quickstart/{sandbox_id}"

    if not _SANDBOX_ID.match(sandbox_id):
        print(
            f"sandbox id {sandbox_id!r} is not usable in a URL. "
            "Use letters, digits, and . _ : - only.",
            file=sys.stderr,
        )
        return 2

    try:
        print(f"1. resolve  {sandbox_id}")
        # `resolve` places the sandbox on a worker and returns the generation
        # to use from here on. Calling it for an existing sandbox is how a
        # client reconnects after a restart: the id is stable, the route is not.
        route = client.call(
            "POST",
            "/api/v1/sandboxes/resolve",
            body={"sandbox_id": sandbox_id, "workspace_scope_id": scope},
        )
        generation = route["generation"]
        step(f"worker={route['worker_id']} generation={generation} status={route['status']}")

        print("2. connect")
        client.call("POST", f"/api/v1/sandboxes/{sandbox_id}", body={"generation": generation})
        step("ready")

        print("3. exec")
        result = client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox_id}/exec",
            body={
                "exec_id": "hello",
                "generation": generation,
                "argv": ["sh", "-lc", "echo hello from $(hostname); python3 -V"],
            },
        )
        step(f"exit={result['exit_code']} stdout={result['stdout'].strip()!r}")
        if result["exit_code"] != 0:
            step(f"stderr={result['stderr'].strip()!r}")

        print("4. files: mkdir, write, list, read, move")
        client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox_id}/files/mkdir",
            body={"generation": generation, "path": "/workspace/notes", "parents": True},
        )
        client.call(
            "PUT",
            f"/api/v1/sandboxes/{sandbox_id}/files",
            body={
                "generation": generation,
                "path": "/workspace/notes/todo.txt",
                "content_base64": base64.b64encode(b"write the thing\n").decode(),
            },
        )
        listing = client.call(
            "GET",
            f"/api/v1/sandboxes/{sandbox_id}/files/list",
            query={"path": "/workspace/notes", "generation": generation},
        )
        step(f"listing={[entry['name'] for entry in listing['entries']]}")
        content = client.call(
            "GET",
            f"/api/v1/sandboxes/{sandbox_id}/files",
            query={"path": "/workspace/notes/todo.txt", "generation": generation},
        )
        step(f"read back {base64.b64decode(content['content_base64'])!r}")
        client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox_id}/files/move",
            body={
                "generation": generation,
                "source": "/workspace/notes/todo.txt",
                "destination": "/workspace/notes/done.txt",
            },
        )
        step("moved todo.txt -> done.txt")

        print("5. cleanup inside the sandbox")
        # `recursive` is opt-in: without it a non-empty directory is refused,
        # so a mistyped path cannot take a whole tree with it.
        client.call(
            "POST",
            f"/api/v1/sandboxes/{sandbox_id}/files/delete",
            body={"generation": generation, "path": "/workspace/notes", "recursive": True},
        )
        step("removed /workspace/notes")

        if args.keep:
            print(f"\nSandbox {sandbox_id} is still running. Release it with:")
            print("  curl -X DELETE -H 'Authorization: Bearer $SANDBOX_INTERNAL_TOKEN' \\")
            print(f"       {args.base_url}/api/v1/sandboxes/{sandbox_id}")
            return 0

        print("6. release")
        client.call("DELETE", f"/api/v1/sandboxes/{sandbox_id}")
        step("released; its workspace is gone")

    except SystemExit as exc:
        # A failed request must not leave the sandbox behind, or repeated runs
        # would accumulate them.
        if not args.keep:
            print(f"\nFailed ({exc}). Releasing {sandbox_id} before exiting.")
            try:
                client.call("DELETE", f"/api/v1/sandboxes/{sandbox_id}")
            except SystemExit:
                pass  # never resolved, so there is nothing to release
        raise
    except KeyboardInterrupt:
        if not args.keep:
            print(f"\nInterrupted. Releasing {sandbox_id}.")
            try:
                client.call("DELETE", f"/api/v1/sandboxes/{sandbox_id}")
            except SystemExit:
                pass
        return 130

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
