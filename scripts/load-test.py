#!/usr/bin/env python3
"""Measure what one worker actually sustains, so sizing is not guesswork.

    # A modest run: 50 sandboxes, 300 commands, 16 at a time.
    export SANDBOX_INTERNAL_TOKEN=...
    ./scripts/load-test.py --sandboxes 50 --execs 300 --concurrency 16

    # Also sample the worker container while the load runs. The name depends on
    # the compose project, which is the directory's name, so ask compose for it.
    ./scripts/load-test.py --sandboxes 100 \
        --container "$(docker compose ps --format '{{.Name}}' agent-sandbox)"

Everything it creates is released at the end unless --keep is passed, and a
failure part-way through still releases what was created.

Two numbers matter for capacity planning and they are different numbers:

* **Sandbox density** — how many idle sandboxes one worker holds. An idle
  sandbox is a directory tree on disk and one registry row; it costs almost no
  memory, so this is bounded by disk and by how many PIDs a peak of concurrent
  commands needs.
* **Command concurrency** — how many commands run at once. Each is a Bubblewrap
  process subtree, so this is bounded by CPU, PID limits, and
  `SANDBOX_MAX_PARALLEL_EXECS_PER_SANDBOX`.

The run reports both, plus the latency distribution, because a mean hides the
tail an agent actually feels.

Commands are sent with an `exec_scope`, so they share a sandbox the way two
agent threads would. A command without a scope is lifecycle-level and takes
the sandbox exclusively; firing those concurrently at one sandbox is supposed
to be refused, and counting that refusal as a failure would misreport
throughput. Pass --exclusive to measure that path instead, where conflicts
are reported separately from errors.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any


class Client:
    def __init__(self, base_url: str, token: str, timeout: float) -> None:
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
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = response.read()
        return json.loads(payload) if payload else None


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * fraction))
    return ordered[index]


def container_sample(name: str) -> dict[str, str] | None:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}\t{{.PIDs}}", name],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        memory, pids = out.stdout.strip().split("\t")
        return {"memory": memory, "pids": pids}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url", default=os.environ.get("SANDBOX_BASE_URL", "http://127.0.0.1:8080")
    )
    parser.add_argument("--token", default=os.environ.get("SANDBOX_INTERNAL_TOKEN"))
    parser.add_argument("--sandboxes", type=int, default=50)
    parser.add_argument("--execs", type=int, default=300)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--container", default=None, help="worker container to sample via docker stats"
    )
    parser.add_argument("--keep", action="store_true", help="leave sandboxes running")
    parser.add_argument(
        "--exclusive",
        action="store_true",
        help="send lifecycle-level commands (no exec_scope), which take a sandbox "
        "exclusively; concurrent sends to one sandbox are then refused by design",
    )
    args = parser.parse_args()

    if not args.token:
        print("Set SANDBOX_INTERNAL_TOKEN (or pass --token).", file=sys.stderr)
        return 2

    if args.container and container_sample(args.container) is None:
        print(
            f"nothing named {args.container!r} is visible to `docker stats`, so its "
            "memory and PID counts cannot be sampled.\n"
            "`docker compose ps` lists the names this host has; "
            "`docker compose ps --format '{{.Name}}' agent-sandbox` prints the one "
            "you want.\n"
            "Omit --container to measure throughput without the resource sections.",
            file=sys.stderr,
        )
        return 2

    client = Client(args.base_url, args.token, args.timeout)
    run = uuid.uuid4().hex[:6]
    created: list[tuple[str, int]] = []

    def release_all() -> None:
        for sandbox_id, _ in created:
            try:
                client.call("DELETE", f"/api/v1/sandboxes/{sandbox_id}")
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                pass

    before = container_sample(args.container) if args.container else None
    failed_commands = 0

    try:
        print(f"==> creating {args.sandboxes} sandboxes")
        started = time.monotonic()
        for index in range(args.sandboxes):
            sandbox_id = f"load-{run}-{index:04d}"
            route = client.call(
                "POST",
                "/api/v1/sandboxes/resolve",
                body={"sandbox_id": sandbox_id, "workspace_scope_id": f"load/{run}"},
            )
            generation = route["generation"]
            client.call("POST", f"/api/v1/sandboxes/{sandbox_id}", body={"generation": generation})
            created.append((sandbox_id, generation))
        create_seconds = time.monotonic() - started

        density = container_sample(args.container) if args.container else None
        if density and before:
            print(f"    container before: {before['memory']}, {before['pids']} PIDs")
            print(f"    container after:  {density['memory']}, {density['pids']} PIDs")

        overview = client.call("GET", "/api/v1/admin/overview")
        print(
            f"    {len(created)} sandboxes in {create_seconds:.1f}s "
            f"({len(created) / create_seconds:.0f}/s); "
            f"fleet reports {overview['sandbox_total']} total, "
            f"capacity {overview['capacity_total']}"
        )

        print(f"==> running {args.execs} commands, {args.concurrency} at a time")
        latencies: list[float] = []
        failures = 0
        conflicts = 0
        problems: list[str] = []
        # A count nobody can act on is not a result. The first few are kept with
        # the id and the answer, because "failures: 1" out of six hundred leaves
        # a reader with nothing to look up.
        reported = 5

        def failed(index: int, reason: str) -> None:
            nonlocal failures
            failures += 1
            if len(problems) < reported:
                problems.append(f"load-{index:04d}: {reason}")

        def one(index: int) -> None:
            nonlocal conflicts
            sandbox_id, generation = created[index % len(created)]
            body: dict[str, Any] = {
                "exec_id": f"load-{index:04d}",
                "generation": generation,
                "argv": ["/bin/sh", "-c", "printf x"],
            }
            # A scope lets a command run alongside others in the same sandbox.
            # Without one it is lifecycle-level and exclusive, so a concurrent
            # send is refused with SANDBOX_EXEC_SCOPE_BUSY — correct behavior,
            # and not something to report as an error. Each command gets its
            # own scope so the run measures how many can genuinely overlap
            # rather than how many fit in one serialized scope.
            if not args.exclusive:
                body["exec_scope"] = f"exec-{index:05d}"
            start = time.monotonic()
            try:
                result = client.call("POST", f"/api/v1/sandboxes/{sandbox_id}/exec", body=body)
                if result.get("exit_code") != 0:
                    failed(
                        index,
                        f"exit {result.get('exit_code')} {str(result.get('stderr') or '')[:120]}",
                    )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")
                if exc.code == 409 and "SANDBOX_EXEC_SCOPE_BUSY" in detail:
                    conflicts += 1
                else:
                    failed(index, f"{exc.code} {detail[:120]}")
            except (urllib.error.URLError, TimeoutError) as exc:
                failed(index, f"{type(exc).__name__}: {exc}")
            latencies.append(time.monotonic() - start)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(one, range(args.execs)))
        elapsed = time.monotonic() - started

        ok = [value for value in latencies if value > 0]
        print(f"    {args.execs} commands in {elapsed:.1f}s -> {args.execs / elapsed:.0f} cmd/s")
        if ok:
            print(
                f"    latency ms: p50={percentile(ok, 0.50) * 1000:.0f} "
                f"p95={percentile(ok, 0.95) * 1000:.0f} "
                f"p99={percentile(ok, 0.99) * 1000:.0f} "
                f"max={max(ok) * 1000:.0f} mean={statistics.fmean(ok) * 1000:.0f}"
            )
        if conflicts:
            print(f"    scope conflicts (409, by design): {conflicts}")
        print(f"    failures: {failures}")
        failed_commands = failures
        for problem in problems:
            print(f"    failed {problem}")
        if failures > len(problems):
            print(f"    ({failures - len(problems)} more failures not shown)")

        peak = container_sample(args.container) if args.container else None
        if peak:
            print(f"    container under load: {peak['memory']}, {peak['pids']} PIDs")

    finally:
        if not args.keep:
            print("==> releasing")
            release_all()
            after = container_sample(args.container) if args.container else None
            if after and before:
                print(f"    container after release: {after['memory']}, {after['pids']} PIDs")

    # Non-zero when commands failed: the numbers above are a sizing measurement,
    # and a run that lost six hundredths of its commands did not measure a
    # healthy worker. Scope conflicts are not failures -- they are the design.
    return 1 if failed_commands else 0


if __name__ == "__main__":
    sys.exit(main())
