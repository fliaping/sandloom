# Integration testing

The unit suite runs against SQLite and in-process fakes. That is fast, but it
cannot observe the behavior this project actually depends on: row-lock semantics
under concurrent admission, how each SQL dialect round-trips a `JSON` column,
Redis TTL expiry, S3 presigned URLs, or Linux namespaces. Those are verified
here, against real servers.

## Running everything

```bash
./scripts/integration-test.sh            # all three of the below
./scripts/integration-test.sh middleware # MySQL, PostgreSQL, Redis, S3 only
./scripts/integration-test.sh sandbox    # Bubblewrap + templates, in a Linux container
./scripts/integration-test.sh polyglot   # Go, Rust and the JDK, as a deployment
./scripts/integration-test.sh down       # stop and remove the stack
```

Any pytest argument after the subcommand is forwarded, so
`./scripts/integration-test.sh middleware -k redis -v` works.

`polyglot` takes the longest, because it builds an image carrying a Go
toolchain, a Rust toolchain and a JDK. It exists because that image is the only
home of three of the five documented languages, and since the rest of the suite
verifies only `python` and `node`, it would stay green while they stopped
working.

It first verifies every language at explicit `basic`, with Docker's default
system-path masking intact and no procfs in the sandbox. This includes Java
JAR execution/Maven validation and Rust Cargo build scripts, local dependencies,
unit tests, doctests, and documentation. A second run checks the service over
HTTP with higher isolation Levels available.

## What each backend proves

| Backend | Service | Verified behavior |
| --- | --- | --- |
| MySQL 8.4 | `compose.integration.yaml` port 13306 | `SELECT ... FOR UPDATE` under `READ COMMITTED`, `JSON` round-trip, CAS state machine |
| PostgreSQL 16 | port 15432 | The same contract on a dialect with different gap-lock behavior |
| Redis 7 | port 16379 | Worker registration, real TTL expiry, concurrent heartbeats, snapshot truncation |
| S3 (LocalStack) | port 19000 | Put/get/delete, presigned GET and PUT fetched over HTTP, binary integrity, chunked download |
| Bubblewrap | `Dockerfile.test` | UID drop, workspace isolation between tenants, `no_new_privs`, private `/tmp`, read-only system mounts, PID namespace, rlimits |
| Templates | `Dockerfile.test` | A real `python -m venv` tree built in one sandbox and executed from another, read-only enforcement, cache sharing between sandboxes, opt-in mounting, digest reuse, survival across a worker restart |

The metadata tests are parametrized across MySQL and PostgreSQL, so every
assertion runs twice — once per dialect. A backend that is unreachable causes a
**skip**, not a failure, so `uv run pytest` stays green on a machine without
Docker.

## Why the isolation tests need a container

The Bubblewrap assertions require a Linux kernel and root: dropping to a sandbox
UID is precisely the behavior under test. On macOS or Windows they skip, so
`Dockerfile.test` gives them somewhere to actually run. It reuses
`agent-sandbox-base`, which means the `bwrap` binary under test is the same one
the production image ships, and the probe therefore negotiates the same level.

Two runtime flags are load-bearing:

- `--cap-add SYS_ADMIN` lets `bwrap` create namespaces. The suite proves the
  sandbox boundary holds even when the runtime process has this capability.
- `--security-opt systempaths=unconfined` is required because Docker masks paths
  under `/proc` by default, which makes `bwrap --proc /proc` fail. Without it the
  probe silently falls back to `basic` and the PID and cgroup namespace tests
  stop testing anything.

The template tests run in the same container for the same reason. Their central
assertion — that a virtualenv built in one sandbox executes in another — depends
on a read-only bind mount landing at the path the venv recorded in its shebangs,
which only a real `bwrap` invocation can demonstrate. Unit tests cover the
archive and cache logic; they cannot show that the resulting tree is runnable,
and in practice two defects that made the feature completely non-functional were
caught only by executing the interpreter.

## Notes on the compose stack

Databases use `tmpfs` for their data directories: the suite drops and recreates
the schema per test, so persistence would only slow it down.

Any S3-compatible store can stand in for LocalStack through the
`SANDBOX_TEST_MINIO_*` variables below; the suite also passes against RustFS
(`rustfs/rustfs`, started with its access-key and secret-key variables, data
directory writable by UID 10001).

LocalStack is pinned to a community release. MinIO's Docker Hub images now
require authentication to pull, and LocalStack's `stable` and `latest` tags
resolve to the Pro image, which exits at startup without a license token.

## Pointing at your own servers

Every endpoint is overridable, so the suite can run against a staging
environment instead of the local stack:

```bash
export SANDBOX_TEST_MYSQL_URL="mysql+aiomysql://user:pw@host:3306/db"
export SANDBOX_TEST_POSTGRES_URL="postgresql+asyncpg://user:pw@host:5432/db"
export SANDBOX_TEST_REDIS_URL="redis://host:6379/0"
export SANDBOX_TEST_MINIO_ENDPOINT="https://s3.example.com"
export SANDBOX_TEST_MINIO_ACCESS_KEY=... SANDBOX_TEST_MINIO_SECRET_KEY=...
```

These tests drop and recreate tables, so never point them at a database that
holds anything you want to keep.

## Local verification snapshot — 2026-10-08

The Sandloom 0.2.0 application/runtime working tree was verified on macOS
with disposable Linux arm64 Docker workers. This is a local validation
record, not a signed release or independent security assessment.

- Main application/runtime suite: **763 passed, 25 skipped** on macOS.
  Those 25 Linux/root-only Bubblewrap/template tests were then run in the
  Linux test image: **25 passed**, with no added SYS_ADMIN capability.
- A newly built service image, without source bind mounts: **51 passed,
  0 failed, 0 skipped** through the authenticated HTTP/MCP deployment verifier.
- Explicit basic, standard and strict: Python, JavaScript, Go, Java and Rust
  compiled/executed in both plain and login shells. Java JAR execution and
  offline Maven validation passed; Rust Cargo local dependencies, build
  scripts, unit tests, doctests and documentation passed. TypeScript's
  optional compiler was not installed in this particular base image.
- Basic used the default masked system paths with only outer seccomp
  unconfined; standard/strict additionally required unmasked system paths.
  Each selected Level was tested explicitly, not inferred from auto.
- Doctor accepted basic with required cgroup namespace and explained why
  optional PID/proc isolation was skipped. Explicit strict in that masked
  environment returned blocked/exit 2; its alternative inventory did not
  silently change the requested policy.
- Ruff and mypy passed; both wheel distributions installed and imported in
  a fresh environment. Compose defaults/overrides, source scanning and
  image project-content/configuration/history scanning passed.

The locally built service image ID was
`sha256:f25e3eca09f264d895242d734f96619349ba34bffdcf1be2ee142eb71bc4fca6`.
This is a local image ID, not a registry manifest digest. Base-image identity,
dependency findings and clean release-commit provenance must still be
recorded when actually publishing; repeat these checks for that commit and
each platform being advertised. The marker scan is not a vulnerability scan.
