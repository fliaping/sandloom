# Deploying Sandloom

Sandloom is a Linux service. macOS and Windows users should run its Linux
worker image through their container runtime, not run Bubblewrap on the host.
Start with one worker; add middleware only when the deployment needs it.

## What to prepare

| Deployment | Metadata | Worker discovery | Live workspaces | Object storage |
| --- | --- | --- | --- | --- |
| Local evaluation / one worker | SQLite in the named volume | In-memory | Local POSIX volume | Not required |
| One enterprise worker | SQLite, PostgreSQL or MySQL | In-memory | Persistent POSIX volume | Optional S3 for templates/checkpoints |
| Multiple workers | Shared PostgreSQL or MySQL | Shared Redis | Per-worker POSIX; shared RWX POSIX for automatic recovery | S3-compatible for cross-worker templates/checkpoints |

Redis is not a database substitute. S3 stores archives, **not** the live
`/workspace` filesystem. With local-only workspaces, worker loss does not
automatically restore workspace contents; checkpoint/restore orchestration
belongs to the caller. A shared volume must preserve Unix ownership and
permissions. SQLite and the in-memory registry are single-worker defaults.

Prepare a strong API token, sufficient disk capacity, an operator-approved
Linux namespace policy, and persistent storage. The service runs as root
**inside the trusted worker** to allocate distinct sandbox UIDs. Do not mount
the Docker socket or enable privileged mode. Sandbox processes drop their UID
and run inside Bubblewrap. See [Isolation](ISOLATION.md) for the actual boundary.

## 1. Build and start a single worker

Use a recent Docker Compose v2 with support for optional `env_file` entries.
The default image contains Python and Node; build the polyglot variant below
for Java, Rust and Go.

```bash
cp .env.example .env
chmod 600 .env
export SANDBOX_INTERNAL_TOKEN="$(openssl rand -base64 32)"
docker build -f Dockerfile.base -t agent-sandbox-base:latest .
docker compose up -d --build --wait
curl --fail http://127.0.0.1:8080/healthz
```

This builds from the checkout; it does not assume an unpublished public image
exists. The exported token lasts for the current shell only. Store it in your
deployment secret manager or the protected `.env` for later restarts; never
commit it. The image installs all built-in database/Redis/S3 drivers.

`.env` reaches the container. Compose explicitly interpolates database,
registry, isolation and network settings so edits to those values take effect.
The internal listen port remains 8080; use `SANDBOX_HTTP_PORT` to change the
host port. The named volume pins the local workspace and the default SQLite
database under `/var/lib/agent-sandbox`. Its legacy name is retained for
deployment compatibility. A bare Python installation instead needs a writable
database URL, e.g. `sqlite+aiosqlite:///./data/control.db`.

Only `127.0.0.1` is published by default. For remote clients, use a protected
TLS gateway and firewall; explicitly choose `SANDBOX_BIND_ADDRESS` if needed.
Do not expose this shared-token control plane directly as an anonymous public
service. `/admin` is static HTML; its data/actions require the API token.
Restrict `/healthz`, documentation and internal routes at the gateway too.

## 2. Diagnose the worker environment

```bash
# Works before starting the HTTP server; no DB/Redis/S3 connection is needed.
docker compose run --rm --no-deps agent-sandbox python -m agent_sandbox.doctor --json

# Inspect the exact running image and container policy.
docker compose exec agent-sandbox python -m agent_sandbox.doctor --json

# End-to-end authenticated API check after startup.
python scripts/verify-deployment.py --base-url http://127.0.0.1:8080 --strict
```

Doctor exits 0 for a usable requested execution policy, 2 when blocked. It is
not a middleware health check or security certification. Run it on each worker
class and compare the effective profile with `/healthz`.
[Agent-assisted diagnosis](ENVIRONMENT_DIAGNOSTICS.md) explains how to choose
required and optional additions without changing baseline guarantees.

The quick start permits nested namespace syscalls with
`seccomp=unconfined`. This widens the trusted outer manager's syscall access;
it is not a blanket production recommendation. Default outer policies can
block **all** Levels. Masked proc paths commonly prevent standard/strict even
when basic works. Adopt a tested narrow policy where your platform supports
one; do not let an Agent silently grant host privileges to make a probe pass.

## 3. Java, Rust and Go

```bash
docker build -f Dockerfile.polyglot -t agent-sandbox-base:polyglot .
docker compose build --build-arg BASE_IMAGE=agent-sandbox-base:polyglot
# Set these in .env before restarting:
# SANDBOX_TOOLCHAINS=python,node,go,java,rust
# SANDBOX_ISOLATION_LEVEL=basic
docker compose up -d --wait
```

The polyglot base builds on the base image from step 1. Its image-owned Java
and Rust launchers support basic without procfs. Compilers/runners are image
content; adding a toolchain name does not install it. TypeScript uses Node but
requires a compiler/runner supplied by your custom image or template.
Launch checks do not prove every build framework or dependency works. See
[Toolchains](TOOLCHAINS.md) for the tested workflows and launcher limitations.

## 4. Enterprise middleware

Use private addresses and independently provisioned databases. Replace the
placeholder credentials through your secret manager:

```dotenv
SANDBOX_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@DB_HOST:5432/sandloom
# Or: mysql+aiomysql://USER:PASSWORD@DB_HOST:3306/sandloom
SANDBOX_DATABASE_AUTO_DDL=false
SANDBOX_REGISTRY_BACKEND=redis
SANDBOX_REDIS_URL=redis://REDIS_HOST:6379/0
SANDBOX_REGISTRY_NAMESPACE=sandloom-production
SANDBOX_OBJECT_STORE_BACKEND=s3
BLOBSTORE_BUCKET=YOUR_BUCKET
BLOBSTORE_REGION=us-east-1
BLOBSTORE_BASE_PREFIX=sandloom-production/
# Set BLOBSTORE_ENDPOINT only for a non-default S3-compatible endpoint.
```

For one worker keep `SANDBOX_REGISTRY_BACKEND=memory`. Redis is needed for
multiple workers. Create the SQL schema with your controlled deployment step
before starting with `DATABASE_AUTO_DDL=false`; evaluation can use true.
See [Operations](OPERATIONS.md) for schema checks and upgrade constraints.
Use the standard AWS credential chain/workload identity where available;
`BLOBSTORE_ACCESS_KEY`/`BLOBSTORE_SECRET_KEY` are optional alternatives.
Provision the bucket and least-privilege read/write access in advance.

Each worker must advertise an address peers can reach and use the same API
token, SQL database and Redis namespace. Workers serving the same placement
pool need matching **effective** profile hashes and toolchain images. For
shared workspaces, mount the same RWX POSIX storage and set
`SANDBOX_SHARED_ROOT` to the mounted path on every worker. Namespace isolation
does not enforce aggregate per-sandbox CPU/memory/disk quotas; budget the
outer worker resources and consult [Sizing](SIZING.md).

Private enterprise adapters are separately versioned wheels layered onto the
public image; do not place them in this checkout or public build context.
Their entry points use the existing `agent_sandbox.*` plugin groups.
[Private adapters](PRIVATE_ADAPTERS.md) gives the migration and compatibility
procedure; changing the public brand does not rename those interfaces.

## 5. A disposable two-worker example

Use the full [Fleet verification](INTEGRATION_TESTING.md) recipe, including its
shortened reaper periods and `other-profile` worker. The test command is
`uv run python scripts/verify-fleet.py --strict --short-graces`. It stops a
worker and checks data reclamation: run it **only on a disposable test fleet**,
never a production deployment.

The example starts PostgreSQL, Redis, LocalStack S3 and workers on ports
18081/18082; the workers initialize the development bucket at startup. The
SQL/S3 test data is ephemeral and credentials are
development-only. This is a runnable integration example, **not** an HA
production stack. Do not use its tmpfs databases or demo keys in production.
[Fleet verification](INTEGRATION_TESTING.md) covers its complete commands.

## Operations and upgrades

- Back up SQL metadata, live POSIX workspaces and required S3 artifacts. A
  database backup alone is not a workspace backup.
- Stop new work and let active executions finish before worker replacement;
  record effective profiles and toolchains. See [Operations](OPERATIONS.md)
  for the current shutdown behavior and lack of a manual drain API.
- Keep image digests and adapter versions pinned. Validate an upgrade against
  a backed-up copy, and apply controlled schema changes before restart.
- `docker compose down` preserves named volumes. Do not use `down -v` unless
  deliberately discarding the data.
- Monitor authenticated worker health, disk watermarks and failed executions.
  Root namespace privileges, TLS ingress, backup recovery and image
  vulnerability scanning remain deployment responsibilities.

See [Admin console](ADMIN_CONSOLE.md), [Operations](OPERATIONS.md),
[Configuration](CONFIGURATION.md), and [Open-source release](OPEN_SOURCE_RELEASE.md).
