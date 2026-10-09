# Operating Sandloom

Keep the worker manager trusted, restrict its shared API token, and budget the
outer container independently of namespace isolation. Start with
[Deployment](DEPLOYMENT.md) and [Sizing](SIZING.md).

## Health and environment evidence

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail -H "Authorization: Bearer $SANDBOX_INTERNAL_TOKEN" \
  http://127.0.0.1:8080/internal/v1/health
docker compose exec agent-sandbox python -m agent_sandbox.doctor --json
```

Liveness is not middleware readiness. Compare the effective profile hash,
selected Level/additions, toolchain checks, disk watermarks and heartbeat
freshness. The console's Fleet view shows actual results and skipped optional
capabilities. Restrict unauthenticated health/doc endpoints at the gateway;
probe errors may disclose host paths and runtime versions.

## Managed SQL schemas and upgrades

SQLite with automatic DDL is the single-worker evaluation default. For a
managed PostgreSQL/MySQL database, provision a dedicated schema and apply the
reviewed `deploy/sql/generic-postgresql.sql` or
`deploy/sql/generic-mysql.sql` through your normal database deployment process.
Then set `SANDBOX_DATABASE_AUTO_DDL=false`. Do not grant runtime schema-owner
permissions merely to avoid a controlled migration.

These files create a fresh schema; they are **not incremental migrations**.
Reapplying `CREATE TABLE IF NOT EXISTS` does not add columns to an existing
table. Back up the database, compare the new schema and apply reviewed ALTER
statements before restarting upgraded workers. Startup refuses missing tables
or columns (exit 3); extra columns are warnings to permit compatible rollbacks.
Inspect [Configuration](CONFIGURATION.md#metadata) for the actual checks.

Test upgrades/rollbacks against a copy of metadata and workspace data. Pin
the public image and private adapter versions. Application and runtime
distributions move together; do not mix their versions. Changed isolation
capabilities change the effective profile and fleet placement compatibility.

## Worker replacement

The current service has no operator drain API that guarantees zero-loss
replacement. `DRAINING` in health describes disk availability, not a manual
drain command. Stop new work through the caller/gateway, wait for executions
to finish, and checkpoint or otherwise preserve needed workspaces before
replacement. Shutdown cancels remaining background work and unregisters the
worker; it does not promise every in-flight command completes.

Sandbox routes are sticky. Local-only workspace data is not automatically
recovered onto another worker. Shared RWX POSIX storage must preserve UID
ownership; S3 checkpoints require caller-driven restore orchestration. The
exception is a suspended sandbox whose suspend uploaded a snapshot: resuming it
restores the workspace on another worker ([Suspend and resume](SUSPEND_RESUME.md)).
Client requests must carry the current generation so stale owners are refused.

## Backups and recovery

- Preserve SQL metadata, required live POSIX workspace data and S3 artifacts;
  none substitutes for the others. Coordinate backups for a consistent point
  in time, and actually test a restore.
- Use SQLite's supported backup mechanism or quiesce the service; copying only
  the database file while WAL writes run is not a consistent backup.
- Keep tokens, database credentials and private adapter configuration in a
  separate secret manager. Do not bundle them into a public backup or screenshot.
- `docker compose down` retains named volumes. `down -v` destroys them; reserve
  it for deliberate disposal of development/test data.

## Incident boundaries

When a required capability fails, retain the blocked policy and inspect outer
kernel/seccomp/LSM/system-path settings. Do not auto-grant privileged mode,
SYS_ADMIN or the Docker socket. Optional failure can be a legitimate supported
profile, not an incident. Re-run doctor and authenticated API verification
after an operator-approved change. See
[Environment diagnosis](ENVIRONMENT_DIAGNOSTICS.md).

Do not run `verify-fleet.py` against production: it deliberately stops a worker
and exercises reclamation. Image vulnerability scans, TLS ingress, database
HA, backup retention and independent escape review remain deployment work.
