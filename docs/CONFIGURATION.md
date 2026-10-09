# Configuration

Every setting below is an environment variable with the `SANDBOX_` prefix, read
by `Settings` in [`src/agent_sandbox/config.py`](../src/agent_sandbox/config.py).
The process environment wins; a `.env` file in the working directory is read as
a fallback, which is the arrangement the quick start's `cp .env.example .env`
sets up.

Three things are worth knowing before reading the tables:

- **A name that does not exist is ignored.** The model is configured with
  `extra="ignore"`, so a misspelled variable starts the service with the default
  still in place, and nothing reports it. If a setting appears to have no
  effect, check its spelling here first.
- **A list is written `a,b,c`.** One value is a list of one, and a JSON array is
  still accepted. That applies to `SANDBOX_TOOLCHAINS`,
  `SANDBOX_READONLY_MOUNTS`, `SANDBOX_HTTP_PROXIES`, `SANDBOX_MCP_ALLOWED_HOSTS`,
  `SANDBOX_EGRESS_DENIED_ADDRESSES` and `SANDBOX_EGRESS_ALLOWED_LITERALS`.
- **A malformed value is refused at startup**, not at the first request that
  needs it. [Startup validation](#startup-validation) lists what is checked.

`GET /healthz` publishes what the worker negotiated — the isolation level, the
profile hash other replicas compare before they place work here, and the
reclamation counters with the periods they run on — so it is the quickest way to
confirm a change took effect. The periods are reported as effective values, not
as the defaults of the release, which is how `scripts/verify-fleet.py`
`--short-graces` can tell a fleet started with the shortened ones from a fleet
that was not.

## The service

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_HOST` | `0.0.0.0` | Address the HTTP server binds. |
| `SANDBOX_PORT` | `8080` | Port it binds. Checked to be in `1..65535`. The default can be moved by `AUTO_PORT0`; an explicit value here always wins. |
| `SANDBOX_ADVERTISE_HOST` | the machine's FQDN | The host other workers use to reach this one. Outside `local` it must be set and must not be a loopback address, because a loopback endpoint is unreachable from its peers. |
| `SANDBOX_ADVERTISE_SCHEME` | `http` | Scheme in that URL. |
| `SANDBOX_INTERNAL_TOKEN` | empty | The bearer token every API call presents. In `prod` an empty value is a startup error. Elsewhere the service generates a random one and warns; because a generated token is never written down, a deployment that leaves this empty is reachable but not callable, so set it explicitly — the quick start does. |
| `SANDBOX_STARTUP_LOG_PATH` | `/tmp/agent-sandbox-startup.log` | A marker line is appended each time the service becomes ready: `agent-sandbox,status=success,ready,worker_id=...,port=...`. It outlives the process, so an operator can tell a service that started from one that never did. |

## Runtime identity and isolation

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_PROFILE_ID` | `coding-default` | Name reported in the capability document. |
| `SANDBOX_PROFILE_HASH` | `bubblewrap-0.11.2-generic-v12` | The profile's identity. The worker appends the isolation level and the egress mode, and a replica only places sandboxes on workers whose effective hash matches its own, so two replicas with different sandbox settings do not silently share work. |
| `SANDBOX_ISOLATION_LEVEL` | `auto` | `auto`, `basic`, `standard` or `strict`. `auto` picks the strongest level the container permits and falls back monotonically; an explicit level is strict, and the service refuses to start if the host cannot provide it. See [Isolation](ISOLATION.md). |
| `SANDBOX_ISOLATION_REQUIRED_FEATURES` | empty | Additional `pid_namespace` and/or `cgroup_namespace`. Startup probes the actual combination and refuses unavailable requirements. PID always includes private procfs. |
| `SANDBOX_ISOLATION_OPTIONAL_FEATURES` | empty | Same allowlist; unavailable additions are skipped with reasons in `feature_policy.skipped_optional`. Never removes a Level's baseline features. See [Environment diagnostics](ENVIRONMENT_DIAGNOSTICS.md). |
| `SANDBOX_NETWORK_MODE` | `host` | `host` or `isolated`. `isolated` gives the sandbox its own network namespace and no egress. It is separate from the isolation level because it changes what an application can do rather than only strengthening the process boundary. |
| `SANDBOX_EXECUTION_BACKEND` | `bubblewrap` | `bubblewrap`, or an entry point in `agent_sandbox.execution_backends` — see [Adapters](ADAPTERS.md). |
| `SANDBOX_BUBBLEWRAP_PATH` | `/usr/bin/bwrap` | The binary used to build every sandbox. A setuid `bwrap` is refused. |
| `SANDBOX_SETPRIV_PATH` | `/usr/bin/setpriv` | Used to drop privileges. |
| `SANDBOX_PRLIMIT_PATH` | `/usr/bin/prlimit` | Used to apply the resource limits below. |
| `SANDBOX_BASH_PATH` | `/bin/bash` | The shell the runtime composes the sandbox's login `PATH` for. |
| `SANDBOX_BUBBLEWRAP_MIN_VERSION` | `0.11.2` | A host whose `bwrap --version` is below this is refused at startup. The comparison is numeric, so `0.11.10` is newer than `0.11.2`. |

The four binaries above are checked for existence and the executable bit before
the service binds a port.

## Sandbox limits

These become the sandbox's resource limits and mount set. Their defaults are
also the values the profile hash is computed from, so changing one changes the
profile identity.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_TMPFS_BYTES` | `536870912` (512 MiB) | Size of the sandbox's private tmpfs. |
| `SANDBOX_CPU_SECONDS` | `300` | CPU seconds per process (`RLIMIT_CPU`), not a shared sandbox CPU quota or wall-clock deadline. |
| `SANDBOX_MAX_PROCESSES` | `64` | `RLIMIT_NPROC` counts processes/threads for the real UID, subject to OS privilege semantics; not an execution-scoped cgroup PID budget. See `SANDBOX_TOOLCHAINS` for Go's compiler limits. |
| `SANDBOX_MAX_OPEN_FILES` | `512` | Open descriptors per process (`RLIMIT_NOFILE`), not per sandbox. |
| `SANDBOX_MAX_FILE_SIZE_BYTES` | `1073741824` (1 GiB) | Largest individual file (`RLIMIT_FSIZE`), not an aggregate disk quota. |
| `SANDBOX_READONLY_MOUNTS` | `/usr,/bin,/sbin,/lib,/lib64,/etc` | Host trees mounted read-only into every sandbox. |

## Workspace and lifecycle

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_LOCAL_ROOT` | `/var/lib/agent-sandbox/sandboxes` | Where sandbox directories are created. May not be a symlink, and must be writable. |
| `SANDBOX_SHARED_ROOT` | unset | A shared read-write directory (an RWX volume). When set it becomes the workspace root in place of `SANDBOX_LOCAL_ROOT`. |
| `SANDBOX_UID_START` | `20000` | First uid handed to a sandbox. |
| `SANDBOX_UID_END` | `59999` | Last one. Must be at least `SANDBOX_UID_START`, which must be at least 1. |
| `SANDBOX_IDLE_TTL_SECONDS` | `1800` | A sandbox with no activity for this long is reclaimed. |
| `SANDBOX_SUSPENDED_RETENTION_SECONDS` | `604800` (7 days) | A suspended sandbox is kept this long, then released with reason `SUSPEND_EXPIRED` and its directory and snapshot deleted. `0` keeps it until a client releases it. Suspended sandboxes are not subject to the idle TTL. See [Suspend and resume](SUSPEND_RESUME.md). |
| `SANDBOX_SUSPEND_SNAPSHOT` | `auto` | Whether a suspend archives `/workspace`, `/home` and `/envs` to the object store so a resume can land on another worker: `auto` (local storage with a store configured), `always` (refuse to suspend without a store), or `never`. |
| `SANDBOX_WORKER_CAPACITY` | `32` | Sandboxes this worker admits. Placement prefers the worker with the fewest running sessions relative to its capacity, from the last heartbeat. |
| `SANDBOX_HEARTBEAT_INTERVAL_SECONDS` | `10.0` | How often a worker re-registers itself. |
| `SANDBOX_HEARTBEAT_TTL_SECONDS` | `30` | A registration older than this is stale. It must exceed twice the interval, or a single missed heartbeat would look like a dead worker. |
| `SANDBOX_DEFAULT_TIMEOUT_SECONDS` | `300` | Execution timeout when a request does not ask for one. |
| `SANDBOX_TERMINATE_GRACE_SECONDS` | `5.0` | How long a sandbox process is given to exit after termination is requested, before it is killed. |

## Reclamation and admission

Reclamation is described in [Architecture](ARCHITECTURE.md#reclamation).

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_MAINTENANCE_INTERVAL_SECONDS` | `60.0` | Period of the worker's maintenance cycle. Must be at least 1. |
| `SANDBOX_ORPHAN_RUNNING_GRACE_SECONDS` | `60` | A route still in `RUNNING` with no recorded activity for this long becomes a reclamation candidate. Its worker is then checked: if it has left the registry, or come back with a different epoch, the route is released with reason `WORKER_LOST`. |
| `SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS` | `300` | A route already marked `RELEASING` that has not finished within this long is picked up again, so a release interrupted halfway is retried rather than left behind. |
| `SANDBOX_ORPHAN_REAPER_BATCH_SIZE` | `100` | Orphans handled per cycle. The remainder is picked up by the next one. |
| `SANDBOX_DISK_HIGH_WATERMARK_PERCENT` | `90` | Above this disk usage the worker refuses new sandboxes. Must be between 1 and 99. |
| `SANDBOX_MIN_FREE_BYTES` | `1073741824` (1 GiB) | And it refuses them below this much free space. |

## API limits

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_MAX_OUTPUT_BYTES` | `8388608` (8 MiB) | Bounds how much of an execution's stdout and stderr is read back. |
| `SANDBOX_MAX_FILE_API_BYTES` | `33554432` (32 MiB) | Largest file the file API reads or writes in one call. |
| `SANDBOX_MAX_LIST_ENTRIES` | `1000` | Page bound for a directory listing. A listing is paged; this bounds one page so a `node_modules` tree cannot build a million-entry response in memory. |
| `SANDBOX_MAX_PARALLEL_EXECS_PER_SANDBOX` | `16` | Concurrent executions in one sandbox, between 1 and 64. Requests beyond it are refused with `SANDBOX_PARALLEL_EXEC_LIMIT` rather than queued. |
| `SANDBOX_MCP_ALLOWED_HOSTS` | `localhost,localhost:*,127.0.0.1,127.0.0.1:*,test` | `Host` header allowlist for the MCP server's DNS-rebinding protection, so a browser cannot be pointed at it by a name that resolves to the host. `SANDBOX_ADVERTISE_HOST` and that host with the port are always allowed in addition. |

## Metadata

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_DATABASE_URL` | `sqlite+aiosqlite:///./data/agent-sandbox.db` | SQLAlchemy URL. The default is used only when this is empty. |
| `SANDBOX_DATABASE_AUTO_DDL` | unset; `true` for SQLite | Whether the service creates and upgrades its own tables. Set it to `false` when the schema is applied separately from [`deploy/sql`](../deploy/sql); the service then refuses to start on a database that is missing something it needs, naming it, rather than failing on a later request. |
| `SANDBOX_METADATA_BACKEND` | `auto` | `auto` and `sqlalchemy` use the built-in store; anything else is an entry point in `agent_sandbox.metadata_stores`. |
| `SANDBOX_MYSQL_CONNECT_TIMEOUT_SECONDS` | `10` | MySQL connection timeout. |
| `SANDBOX_MYSQL_POOL_RECYCLE_SECONDS` | `300` | Age at which a pooled MySQL connection is replaced, which is what keeps a connection from outliving a server-side idle timeout. |

**Audit retention.** Recorded executions and route lifecycle history are kept
for 15 days (`AUDIT_HISTORY_RETENTION_DAYS`) and pruned hourly on the maintenance
cycle; the entries in `reaper_status` report how many rows each pass removed. A
routine holding an execution's stdout and stderr is the largest thing this store
holds — 8,700 commands of test load left 84 MiB of output against 360 KiB of
routes — so the window is what bounds it. Deleting rows does not return that
space to the filesystem, because SQLite reuses it for later writes; reclaim it
with `VACUUM` when the deployment is quiet:

```bash
sqlite3 /var/lib/agent-sandbox/control.db 'VACUUM;'
```

PostgreSQL's autovacuum does the same job on its own.

The built-in store runs SQLite in WAL mode with a 15-second busy timeout, so
commands that arrive together wait for the write lock instead of failing on it.
Both settings were missing and the cost was measurable rather than theoretical:
at 32 concurrent commands the load test in [Sizing](SIZING.md) answered HTTP 500
from the exec route about once per six hundred commands, every one of them
`database is locked` on a write that had waited the DBAPI's default five
seconds. With them, the same run reports no failures and 31 commands per second
rather than 21. SQLite still admits one writer at a time, so a fleet — several
replicas over one store, or a sustained command rate above what one process
commits — wants PostgreSQL.

## Worker registry

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_REGISTRY_BACKEND` | `memory` | `memory` suits one worker; `redis` is what makes several replicas share placement. Anything else is an entry point in `agent_sandbox.worker_registries`. |
| `SANDBOX_REGISTRY_NAMESPACE` | `agent-sandbox` | Key prefix for the shared registry, so one Redis can serve more than one deployment. |
| `SANDBOX_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis to use when the backend is `redis`. |

## Object storage and templates

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_OBJECT_STORE_BACKEND` | `s3` | `s3`, `disabled`, or an entry point in `agent_sandbox.object_stores`. Setting it to `disabled` makes templates a single-worker feature. |
| `SANDBOX_BLOBSTORE_ENDPOINT` | empty | Endpoint of the S3-compatible store. |
| `SANDBOX_BLOBSTORE_BUCKET` | empty | Bucket. Templates need both this and the endpoint to cross workers, and an S3 deployment without them is only usable on the worker that built the template. The bucket is created at startup when the endpoint does not have it, so a new store needs no preparation. |
| `SANDBOX_BLOBSTORE_BASE_PREFIX` | `agent-sandbox/` | Key prefix, so one bucket can hold more than one deployment. Leading and trailing slashes are normalised. |
| `SANDBOX_BLOBSTORE_REGION` | `us-east-1` | Region passed to the client. |
| `SANDBOX_TEMPLATE_ROOT` | a `templates` sibling of the workspace root | Where materialised templates live. A sibling rather than a child, so no sandbox can reach the cache through its own tree. |
| `SANDBOX_TEMPLATE_CACHE_MAX_BYTES` | unset | Total bytes the cache may use. Unset means no eviction; set it to enable LRU eviction, which never removes a revision a live sandbox is using. The cache is swept on the maintenance cycle, so the bound is enforced within `SANDBOX_MAINTENANCE_INTERVAL_SECONDS` rather than at the moment a template is pulled. |
| `SANDBOX_TEMPLATE_MAX_EXTRACT_BYTES` | `8589934592` (8 GiB) | Largest regular-file byte sum in a snapshot or extracted template. |
| `SANDBOX_TEMPLATE_MAX_ARCHIVE_BYTES` | `4294967296` (4 GiB) | Compressed snapshot output bound, enforced while writing; oversized builds never upload. |
| `SANDBOX_TEMPLATE_SNAPSHOT_MAX_ENTRIES` | `200000` | Total file, directory, and symlink entries scanned per snapshot. Positive; bounds scan memory/work before sorting. |
| `SANDBOX_TEMPLATE_SNAPSHOT_MAX_DEPTH` | `128` | Maximum member path depth, between 1 and 256. |
| `SANDBOX_TEMPLATE_SNAPSHOT_TIMEOUT_SECONDS` | `600` | Positive, finite cooperative snapshot time budget. Checked during scans, reads, and compression; not a hard interrupt for blocked filesystem syscalls or an object-store upload timeout. |

These are adjustable work budgets, not additional host capabilities required
by a particular isolation Level. Snapshot source traversal is no-follow at
every Level, while final symlinks remain valid archive entries. Builds and
template attachment refuse concurrent execution or File API work with a lock
error; callers may retry after it finishes. Release terminates executions,
then drains template operations before removing the source directory.

Request cancellation is not rollback: a build already running in a thread may
complete an immutable revision and publish it. The request drains that worker
before propagating cancellation, so locks and lifecycle guards never imply
that a still-running operation has finished. An invalid or partial archive is
never uploaded or published; failed staging files are removed.

Snapshot encoding has changed with descriptor-relative traversal. An unchanged
tree may therefore receive a different digest than it did with the previous
builder. Existing immutable revisions and references remain readable; this
does not rewrite or republish them.

Each object storage setting is also read under the name the S3 ecosystem
already uses: `BLOBSTORE_ENDPOINT`, `BLOBSTORE_BUCKET`,
`BLOBSTORE_BASE_PREFIX`, `BLOBSTORE_REGION`. Both forms work, and the
`SANDBOX_`-prefixed one wins when both are set, so a deployment can carry one
name for the settings model and one for the tools beside it. See
[Not read from the settings model](#not-read-from-the-settings-model).

## Toolchains and package mirrors

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_TOOLCHAINS` | `python,node` | Which languages the runtime configures, in `PATH` precedence order. An unknown name is refused at startup rather than silently given no environment, and the order decides which toolchain's binaries shadow another's. |
| `SANDBOX_PYTHON_INDEX_URL` | `https://pypi.org/simple/` | Index pip and uv fetch from. |
| `SANDBOX_NPM_REGISTRY` | `https://registry.npmjs.org` | Registry npm fetches from. |
| `SANDBOX_GO_PROXY` | `https://proxy.golang.org,direct` | Module proxy list. |
| `SANDBOX_GO_SUMDB` | `sum.golang.org` | Checksum database. |
| `SANDBOX_CARGO_REGISTRY_URL` | empty | Crates.io mirror, published to the sandbox as a source replacement. Unset leaves Cargo on the default registry. |
| `SANDBOX_MAVEN_REPOSITORY_URL` | empty | Maven mirror, published to the sandbox as `MAVEN_MIRROR_URL`. |
| `SANDBOX_JAVA_HOME` | unset; polyglot image sets `/usr/local/lib/agent-sandbox/java` | JDK-shaped directory of image-owned procfs-free launchers. Supplies `JAVA_HOME` and a `PATH` entry for Java and build tools. Unset uses the system JDK. |
| `SANDBOX_RUST_LAUNCHER_DIR` | unset; polyglot image sets `/usr/local/lib/agent-sandbox/rust/bin` | Directory of procfs-free Rust launchers, inserted before the image's rustup proxies. Unset preserves the legacy proxy path. |

See [Toolchains](TOOLCHAINS.md) for where each language keeps its caches and
binaries.

## Egress and proxying

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_HTTP_PROXIES` | empty | Proxies exported to sandboxes as `HTTP_PROXY`, `HTTPS_PROXY` and their lowercase forms. When several are listed, consecutive executions rotate through them. |
| `SANDBOX_NO_PROXY` | `127.0.0.1,localhost,::1` | Exported as `NO_PROXY` and `no_proxy`. |
| `SANDBOX_EGRESS_DENIED_ADDRESSES` | empty | Validated addresses/CIDRs reserved for an enforcing execution plugin. The built-in backend refuses nonempty values. |
| `SANDBOX_EGRESS_ALLOWED_LITERALS` | empty | Validated literal-IP exceptions for an enforcing execution plugin. The built-in backend refuses nonempty values. |

[Isolation](ISOLATION.md) explains the policy helper and its limitations.
Neither proxy environment variables nor this helper filter the built-in
backend's shared `host` network. Use `isolated` when no network is required.

## Plugins and telemetry

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_CREDENTIAL_BROKER` | `disabled` | `disabled`, or an entry point in `agent_sandbox.credential_brokers`. |
| `SANDBOX_TELEMETRY_SINK` | unset | An entry point in `agent_sandbox.telemetry_sinks`. Unset attaches no handler. |

[Adapters](ADAPTERS.md) has the contracts for all of these, and
[Private adapters](PRIVATE_ADAPTERS.md) covers keeping one out of the published
code.

## Not read from the settings model

These are read directly, because they have to work before or outside `Settings`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SANDBOX_ENVIRONMENT` | `local` | `local`, `staging` or `prod`. Anything else stops the service. It is read before settings are built, because `prod` is what makes an empty `SANDBOX_INTERNAL_TOKEN` a startup error. |
| `SANDBOX_ZONE` | empty | Appended to the environment label that `/healthz` reports. |
| `SANDBOX_REGION` | empty | The same, for the region. |
| `AUTO_PORT0` | empty | When set, it becomes the default for `SANDBOX_PORT` instead of `8080`; a `SANDBOX_PORT` of its own still wins. |
| `BLOBSTORE_ENDPOINT`, `BLOBSTORE_BUCKET`, `BLOBSTORE_BASE_PREFIX`, `BLOBSTORE_REGION` | as above | The unprefixed aliases of the four settings in [Object storage and templates](#object-storage-and-templates). |
| `BLOBSTORE_ACCESS_KEY`, `BLOBSTORE_SECRET_KEY` | empty | Object storage credentials. When unset, the client falls back to the standard boto3 credential chain; a deployment with an endpoint and bucket but no explicit credentials logs a warning at startup rather than failing. |

## Startup validation

A value that cannot work stops the service before it binds a port, and the
message names the variable. The checks, all from
[`src/agent_sandbox/preflight.py`](../src/agent_sandbox/preflight.py):

- `SANDBOX_PORT` is between 1 and 65535.
- `SANDBOX_UID_START` is at least 1, and `SANDBOX_UID_END` is at least
  `SANDBOX_UID_START`.
- `SANDBOX_HEARTBEAT_TTL_SECONDS` is more than twice
  `SANDBOX_HEARTBEAT_INTERVAL_SECONDS`.
- `SANDBOX_WORKER_CAPACITY`, `SANDBOX_IDLE_TTL_SECONDS` and
  `SANDBOX_MAINTENANCE_INTERVAL_SECONDS` are at least 1.
- `SANDBOX_DISK_HIGH_WATERMARK_PERCENT` is between 1 and 99;
  `SANDBOX_MIN_FREE_BYTES` is not negative.
- `SANDBOX_TMPFS_BYTES`, `SANDBOX_MAX_OUTPUT_BYTES`,
  `SANDBOX_MAX_FILE_API_BYTES` and `SANDBOX_DEFAULT_TIMEOUT_SECONDS` are at
  least 1.
- The four binaries in [Runtime identity and
  isolation](#runtime-identity-and-isolation) exist and are executable.
- The workspace root is not a symlink and is writable.
- Outside `local`, `SANDBOX_ADVERTISE_HOST` is set and is not a loopback
  address.

Every rule above has a test that drives it and asserts the message names the
variable, in `tests/test_preflight.py`. Adding a rule here means adding a row
there: a rule the documentation names and nothing exercises is one that can be
deleted without anything failing.

Two things are warnings rather than errors: a container that cannot provide a
user namespace, and object storage with no explicit credentials.
