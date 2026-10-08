# Agent-assisted environment diagnosis

Sandloom keeps `basic`, `standard` and `strict` as stable minimum
contracts. Different kernels and outer policies can support a non-linear
combination, so users can select **additional** capabilities independently.
The Agent diagnoses and proposes; the operator approves permission changes.

## Contracts, additions and network policy

| Setting | Meaning |
| --- | --- |
| `SANDBOX_ISOLATION_LEVEL=auto` | Probe complete Levels and choose the strongest supported one |
| Explicit `basic`, `standard` or `strict` | That complete Level is mandatory; never silently downgraded |
| `SANDBOX_ISOLATION_REQUIRED_FEATURES` | Additions which must pass a real combined execution probe |
| `SANDBOX_ISOLATION_OPTIONAL_FEATURES` | Try additions; retain successful ones and record skipped reasons |
| Network mode `host` or `isolated` | Separate mandatory network policy; not silently changed |

The current additive allowlist is `pid_namespace` and
`cgroup_namespace`. PID isolation always includes **private procfs**; they
cannot be selected separately. Unknown feature names are errors. Required
features are evaluated together first, then optional additions in sorted order
against the already-enabled combination. Baseline features are never removed.
Adding cgroup alone to basic does **not** make it strict.

```dotenv
SANDBOX_ISOLATION_LEVEL=basic
SANDBOX_ISOLATION_REQUIRED_FEATURES=cgroup_namespace
SANDBOX_ISOLATION_OPTIONAL_FEATURES=pid_namespace
```

This configuration can work where cgroup namespaces are permitted but procfs
mounts are masked. The result says `selected_level=basic`,
`profile_mode=custom` and lists the actual features. If PID/procfs is
mandatory, move `pid_namespace` to required or request standard/strict;
the same environment must then refuse startup. Features already guaranteed
by the selected Level are not treated as optional failures.

## Run real probes, not permission guesses

```bash
docker compose run --rm --no-deps agent-sandbox python -m agent_sandbox.doctor --json
# Installed Python distribution on a Linux worker:
sandloom-doctor --json
```

Run **inside the same worker image and container policy** you intend to use,
not on the macOS/Windows host. Doctor uses disposable directories, real
Bubblewrap command launches and toolchain launch checks. It does not start
HTTP, contact SQL/Redis/S3, load private backends, or modify production
workspaces. It inherits configured read-only system mounts/toolchain paths;
review custom mount settings and scripts before executing diagnostics.

The report deliberately excludes tokens, database URLs and credentials.
Probe stderr can still contain host paths and platform versions; review
before publishing it. A missing compiler is reported separately and does not
mean the isolation Level failed. Launch success is not a full build test.

Key JSON fields:

```json
{
  "schema_version": 1,
  "status": "ready",
  "can_start": true,
  "capabilities": {
    "selected_level": "basic",
    "profile_mode": "custom",
    "feature_policy": {
      "required": ["cgroup_namespace"],
      "optional": ["pid_namespace"],
      "enabled_additions": ["cgroup_namespace"],
      "skipped_optional": {"pid_namespace": "procfs mount denied by outer policy"}
    }
  }
}
```

This is an abbreviated illustrative report; actual errors come from the
probe. Exit status is 0 when the requested local execution policy is usable,
2 when blocked. A blocked policy may have a successful `inventory` fallback
that shows alternatives. It remains `can_start=false`: inventory does not
authorize changing that policy.

The service runs the same negotiation at startup. Inspect
`worker.capabilities` in `/healthz` or the authenticated admin overview
after deployment. The effective profile hash includes selected additions,
network mode and nested-user-namespace fallback; differing boundaries do not
silently share a placement pool. Configuration changes require restarting the
worker, not hot-mutating existing sandboxes.

## A safe prompt for a deployment Agent

> Diagnose Sandloom in the target worker using doctor --json. Do not change
> kernel settings, grant privileged mode/CAP_SYS_ADMIN, mount the Docker socket,
> or weaken our requested baseline/network policy. Report supported Levels,
> actual features, failed probes and toolchain results. Propose the smallest
> configuration change consistent with our workload and threat model; separate
> required and optional capabilities. Ask the operator before any permission
> expansion or policy downgrade. Re-run doctor and the authenticated deployment
> verification after an approved change. Never paste credentials into reports.

Doctor is a diagnostic CLI, not an auto-remediation agent. Linux userns,
seccomp, AppArmor/LSM, masked system paths and cgroup support are independently
controlled by the host/runtime. A failed probe alone does not identify which
layer denied it. Diagnose those layers with approved platform tools; do not
automatically execute remediation commands from stderr.

## Limits

No level provides VM separation or an independent kernel. A cgroup namespace
does not allocate CPU/memory/PID quotas. Toolchain compatibility is independent
of Level strength. Private/third-party execution backends must supply their
own diagnostics and capability report; the built-in doctor intentionally
does not import or execute their implementations.

See [Isolation](ISOLATION.md), [Deployment](DEPLOYMENT.md),
[Toolchains](TOOLCHAINS.md), and [Security](../SECURITY.md).
