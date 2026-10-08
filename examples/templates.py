#!/usr/bin/env python3
"""Build a custom environment once, then pull it into another sandbox.

Run it against a service you already started (see the README quick start):

    export SANDBOX_INTERNAL_TOKEN=...      # the value the service was started with
    python examples/templates.py
    python examples/templates.py --base-url http://10.0.0.7:8080

This is the workflow templates exist for. An application's environment — a
Python venv, a `node_modules` tree, a compiler toolchain, anything that lands in
a directory — is built once, published under a name, and mounted read-only into
every sandbox that asks for it, instead of being rebuilt per sandbox.

Two rules matter, and the example follows both:

* **The template mounts at `/envs/<name>`.** So build it at `/envs/<name>` and
  publish it under the same name. A virtualenv keeps its absolute paths in the
  scripts it writes, and a tree built at `/envs/a` and mounted at `/envs/b`
  has a `bin/pip` that points at a path which does not exist.
* **The digest identifies the content.** It is printed after publishing. A pin
  is `name@sha256:...` and keeps working after the name is republished, which is
  what makes a rollback a remount rather than a rebuild.

`SandboxClient` comes from `examples/quickstart.py` so there is one
implementation of the HTTP calls; read that file for the lifecycle in full.
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from quickstart import SandboxClient, step


def _resolve(client: SandboxClient, sandbox_id: str, scope: str) -> int:
    route = client.call(
        "POST",
        "/api/v1/sandboxes/resolve",
        body={"sandbox_id": sandbox_id, "workspace_scope_id": scope},
    )
    client.call("POST", f"/api/v1/sandboxes/{sandbox_id}", body={"generation": route["generation"]})
    return int(route["generation"])


def _release(client: SandboxClient, sandbox_id: str) -> None:
    try:
        client.call("DELETE", f"/api/v1/sandboxes/{sandbox_id}")
    except SystemExit:
        pass  # never resolved, or already gone: nothing to release


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
        "--keep",
        action="store_true",
        help="leave the published template and the sandboxes behind",
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
    suffix = uuid.uuid4().hex[:8]
    builder_id = f"tpl-builder-{suffix}"
    user_id = f"tpl-user-{suffix}"
    # The name has to equal the directory basename, hence the shared suffix: a
    # fixed name would collide with whatever the last run published.
    name = f"demo-env-{suffix}"
    source_path = f"/envs/{name}"
    pinned: dict[str, str] = {}

    try:
        print(f"1. build {source_path} in a sandbox")
        generation = _resolve(client, builder_id, f"templates/{suffix}")
        step("resolved and connected")
        # A real environment, built the way an application would build one. Any
        # tree works; this one is a virtualenv because it needs no network.
        result = client.call(
            "POST",
            f"/api/v1/sandboxes/{builder_id}/exec",
            body={
                "exec_id": "build-env",
                "generation": generation,
                "timeout_seconds": 300,
                "argv": [
                    "sh",
                    "-lc",
                    f"python3 -m venv --system-site-packages {source_path} && "
                    f"printf 'import sys\\nprint(\"running under\", sys.prefix)\\n' "
                    f"> {source_path}/report.py && "
                    f"{source_path}/bin/python {source_path}/report.py",
                ],
            },
        )
        step(f"exit={result['exit_code']} stdout={result['stdout'].strip()!r}")
        if result["exit_code"] != 0:
            step(f"stderr={result['stderr'].strip()!r}")
            raise SystemExit("building the environment failed")

        print("2. publish it as a template")
        published = client.call(
            "POST",
            f"/api/v1/sandboxes/{builder_id}/templates",
            body={
                "generation": generation,
                "name": name,
                "source_path": source_path,
                "description": "created by examples/templates.py",
            },
        )
        pinned["digest"] = published["digest"]
        step(f"name={published['name']} digest={published['digest']}")
        step(f"size={published['size_bytes']} bytes mounts at {published['mount_target']}")

        print("3. pull it into a second sandbox, which never built anything")
        generation = _resolve(client, user_id, f"templates/{suffix}")
        client.call(
            "PUT",
            f"/api/v1/sandboxes/{user_id}/templates",
            # The pin is explicit; a bare `name` would follow the catalog, the
            # way a tag does. Both are accepted, and the pin is what you use to
            # hold a revision still.
            body={"generation": generation, "templates": [f"{name}@{pinned['digest']}"]},
        )
        step(f"attached {name}@{pinned['digest'][:19]}…")
        result = client.call(
            "POST",
            f"/api/v1/sandboxes/{user_id}/exec",
            body={
                "exec_id": "use-env",
                "generation": generation,
                "argv": [f"{source_path}/bin/python", f"{source_path}/report.py"],
            },
        )
        step(f"exit={result['exit_code']} stdout={result['stdout'].strip()!r}")
        if result["exit_code"] != 0:
            raise SystemExit(f"the mounted environment did not run: {result['stderr'].strip()!r}")

        print("4. the mount is read-only")
        # One tree is shared by every sandbox on the worker, so a sandbox that
        # can write into it can change the environment for everyone else.
        denied = client.call(
            "POST",
            f"/api/v1/sandboxes/{user_id}/exec",
            body={
                "exec_id": "write-attempt",
                "generation": generation,
                "argv": ["sh", "-lc", f"touch {source_path}/denied 2>&1; echo rc=$?"],
            },
        )
        step(denied["stdout"].strip())
        if "rc=0" in denied["stdout"]:
            raise SystemExit("a sandbox wrote into a mounted template")

        if args.keep:
            print("\nLeft running:")
            print(f"  template {name} (digest {pinned['digest']})")
            print(f"  sandboxes {builder_id}, {user_id}")
            print("  unpublish with: curl -X DELETE -H 'Authorization: Bearer $SANDBOX_INTERNAL_TOKEN' \\")
            print(f"       {args.base_url}/api/v1/templates/{name}")
            return 0

        print("5. unpublish and release")
        # Unpublishing removes the name from the catalog. Sandboxes on other
        # workers that already mounted it keep their tree until the cache
        # prunes it, so nothing in flight breaks.
        client.call("DELETE", f"/api/v1/templates/{name}")
        step(f"unpublished {name}")
        _release(client, builder_id)
        _release(client, user_id)
        step("released both sandboxes")

    except SystemExit as exc:
        if not args.keep:
            print(f"\nFailed ({exc}). Cleaning up before exiting.", file=sys.stderr)
            try:
                client.call("DELETE", f"/api/v1/templates/{name}")
            except SystemExit:
                pass
            _release(client, builder_id)
            _release(client, user_id)
        raise
    except KeyboardInterrupt:
        if not args.keep:
            print("\nInterrupted. Cleaning up.", file=sys.stderr)
            try:
                client.call("DELETE", f"/api/v1/templates/{name}")
            except SystemExit:
                pass
            _release(client, builder_id)
            _release(client, user_id)
        return 130

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
