# Adapter guide

The public distribution contains portable implementations and stable Python
protocols. Optional packages can replace any external-system boundary without
patching the control plane.

## Built-in adapters

The public `SqlAlchemyDatabase` uses the same table model on SQLite, MySQL, and
PostgreSQL. Common synchronous URLs are normalized to async drivers. SQLite is
intended for one process; MySQL or PostgreSQL should be used for concurrent
control-plane replicas.

- PostgreSQL schema: `deploy/sql/generic-postgresql.sql`
- MySQL schema: `deploy/sql/generic-mysql.sql`

The `memory` registry is local to one process. The `redis` registry supports
multiple replicas. SQL remains authoritative for generation and worker-epoch
fencing.

The S3 adapter supports custom endpoints and the standard boto3 credential
chain. Object storage holds archives rather than filesystem calls: environment
templates, and checkpoint archives a caller keeps. `BlobStore.upload_checkpoint(sandbox_id, archive)`
names one at `checkpoints/<sandbox_id>/<archive name>` and returns the URI that
`get`/`download_to`/`delete` address it by, which is the whole of what this
project does about caller checkpoints — it does not snapshot or restore a running
workspace for you. Suspend is the one exception: it archives a dormant workspace
under `checkpoints/<sandbox_id>/dormant/` so a resume can land on another worker
([Suspend and resume](SUSPEND_RESUME.md)). A metadata store opts in to suspend by
implementing `DormantRouteStore`, and an execution backend by implementing
`DormantLifecycle`; without them the API answers 501
`SANDBOX_SUSPEND_UNSUPPORTED`. See [Architecture](ARCHITECTURE.md).

## Plugin discovery

External Python distributions register factory functions in one or more of
these entry-point groups:

| Contract | Entry-point group | Selector |
| --- | --- | --- |
| `MetadataStore` | `agent_sandbox.metadata_stores` | `SANDBOX_METADATA_BACKEND` |
| `Registry` | `agent_sandbox.registries` | `SANDBOX_REGISTRY_BACKEND` |
| `ObjectStore` | `agent_sandbox.object_stores` | `SANDBOX_OBJECT_STORE_BACKEND` |
| `ExecutionBackend` | `agent_sandbox.execution_backends` | `SANDBOX_EXECUTION_BACKEND` |
| `CredentialBroker` | `agent_sandbox.credential_brokers` | `SANDBOX_CREDENTIAL_BROKER` |
| `TelemetrySink` | `agent_sandbox.telemetry_sinks` | `SANDBOX_TELEMETRY_SINK` |

Example plugin metadata:

```toml
[project.entry-points."agent_sandbox.metadata_stores"]
company-metadata = "company_sandbox_plugin.factories:create_metadata"

[project.entry-points."agent_sandbox.registries"]
company-registry = "company_sandbox_plugin.factories:create_registry"

[project.entry-points."agent_sandbox.object_stores"]
company-objects = "company_sandbox_plugin.factories:create_object_store"

[project.entry-points."agent_sandbox.execution_backends"]
company-runtime = "company_sandbox_plugin.factories:create_runtime"
```

Every entry point must resolve to exactly one callable with this shape:

```python
def create_backend(settings: agent_sandbox.config.Settings) -> Contract:
    ...
```

Discovery is deliberately fail-closed. A missing, duplicated, or non-callable
entry point stops startup instead of silently selecting a public fallback.
Backend names are trusted deployment configuration and are never accepted from
an API request. Selection occurs at process startup; replacing an installed
plugin requires a rolling restart.

## Contract responsibilities

`MetadataStore` owns route, execution, and lifecycle-audit transactions. Its
`describe()` result must contain only health-safe metadata and no credentials.

`Registry` owns heartbeat, lookup, expiry, and load-based selection. Registry
state is advisory and must not bypass SQL fencing.

`ObjectStore` owns namespaced object put/get/delete operations. Private
credential systems belong in this adapter, not in public configuration.

`ExecutionBackend` owns probe, create/get, execute/cancel, file access,
destroy, shutdown, trash cleanup, disk status, and active sandbox/process
indexes. It must preserve generation fencing and report the effective
isolation level.

An execution plugin that only needs environment and mount customization can
reuse the public Bubblewrap implementation with `RuntimeExtension` instead of
copying it. See [Private adapters](PRIVATE_ADAPTERS.md).

## Credential brokers

The `CredentialBroker` protocol decides what a command runs with and which
credentials a caller is allowed to ask for. It runs in the service, once per
execution, before the sandbox is touched.

```python
class CredentialBroker(Protocol):
    def augment_environment(
        self, request: ExecRequest, base_env: dict[str, str]
    ) -> dict[str, str]:
        """Return the environment this execution should run with.

        `base_env` is the environment the caller attached to the request. HOME,
        PATH, the language toolchains and the proxies are added by the runtime
        around it and are reserved: returning one of those fails the execution
        instead of silently overriding the sandbox configuration. Must NOT
        return sensitive values in plain environment variables — point at a
        helper or a provider the sandbox can call instead.
        """
        ...

    def validate_sensitive_keys(self, keys: set[str]) -> None:
        """Allow or refuse the sensitive keys a request named.

        Raises ValueError if any key is not allowed, which the API answers with
        400 and the reason.
        """
        ...
```

What a broker returns is the environment the command runs with, alongside
whatever the caller sent in `env`. That is the whole mechanism: it is the
deployment's own code, so a broker that needs a Git credential helper, a
short-lived token service, or a provider-specific configuration file sets up
whatever it needs and returns the variables that point at it. The values in
`request.sensitive_env` are available to it in the service process, and that is
where a helper should read them from.

Selector: `SANDBOX_CREDENTIAL_BROKER`

Example plugin metadata:

```toml
[project.entry-points."agent_sandbox.credential_brokers"]
company-credentials = "company_sandbox_plugin.factories:create_credential_broker"
```

When `SANDBOX_CREDENTIAL_BROKER=disabled` (default), the service rejects any
`ExecRequest` carrying `sensitive_env`, so a deployment that has not configured
a broker cannot be asked to handle secrets it has no way to handle. The
rejection is a 400 naming the keys, not a silently ignored request.

## Telemetry sinks

A telemetry sink receives every structured log record the service emits, in
addition to the rotating files, so an enterprise log backend can be fed without
a log shipper beside every replica.

The entry point takes the settings and returns a factory that opens the
handler, and the handler is opened on the delivery thread the first time a
record is delivered:

```python
def create_company_sink(settings) -> Callable[[], logging.Handler]:
    def open_handler() -> logging.Handler:
        handler = CompanyLogHandler(endpoint=settings.company_log_endpoint)
        handler.setFormatter(logging.Formatter("%(message)s"))
        return handler

    return open_handler
```

```toml
[project.entry-points."agent_sandbox.telemetry_sinks"]
company-logs = "company_sandbox_plugin.factories:create_company_sink"
```

Selector: `SANDBOX_TELEMETRY_SINK=company-logs`. It is unset by default, and
unset means nothing is loaded.

Two properties are deliberate. The handler is opened lazily, so a sink whose
endpoint is unreachable at boot does not stop the service: records queue to the
files, `delivery_failed` counts the failures, and delivery is retried on a
backoff. And a record reaches the sink unchanged, which is why the sink must not
log from its own `emit` — the pipeline drops records whose thread is its own.

`GET /healthz` reports the state as `logging`, including the configured
`sink_name` and the delivery counters.

## Registry environment filtering

The `Registry.select()` method accepts an optional `environment` parameter to
filter workers by deployment environment:

```python
async def select(
    self,
    *,
    profile_hash: str,
    exclude: set[str] | None = None,
    environment: str | None = None,
) -> dict[str, Any]:
    """Select a worker matching the profile and environment.
    
    When environment is set, only workers with a matching "environment" field
    in their heartbeat payload are considered. This enables multi-environment
    deployments (staging/prod) to share a registry without cross-environment
    scheduling.
    """
    ...
```

Workers must include an `"environment"` field in their heartbeat payload if
environment isolation is required. The field is advisory; SQL generation fencing
remains the authoritative isolation boundary.
