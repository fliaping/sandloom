# Isolation model

Isolation is negotiated from evidence, not inferred from kernel version or
container labels. During startup the worker runs a representative command at
every level and records both supported levels and probe failures in `/healthz`.

## Levels

### basic

- Outer UID separation with cleared supplementary groups.
- `no_new_privs` and process/file/core limits.
- Bubblewrap user, mount, IPC, and UTS namespaces.
- Read-only system mounts and an allowlisted filesystem.
- Private, size-limited `/tmp` and a synthetic `/dev`.

### standard

Adds a PID namespace and private procfs. Processes cannot enumerate or signal
other sandboxes through the outer container's PID namespace.

Some native launchers use `/proc/self/exe` to find their libraries or compiler
sysroot. The current polyglot image adapts the JDK and Rust tools with explicit
library paths and sysroot arguments, so Java and Rust also work at `basic`.
No procfs mount or PID boundary is added to that Level. Applications that
explicitly inspect `/proc` still need a suitable Level.

The worker reports measured tool startup separately from isolation selection.
See [TOOLCHAINS.md](TOOLCHAINS.md#all-shipped-languages-work-at-basic) for the
adapters, version selection, and build verification.

### strict

Adds a cgroup namespace. This level is available only when the kernel and the
outer container runtime allow `CLONE_NEWCGROUP` for the sandbox process.

Network policy is separate:

- `host`: share the worker's network namespace, without an address filter.
- `isolated`: create a private network namespace with no configured interface.

### Resolved-address policy helper (not built-in enforcement)

The built-in Bubblewrap backend does **not** enforce an egress allowlist or
resolved-address filtering. With `host` networking, commands can connect
directly to loopback, the control plane, internal services, and metadata
endpoints. `HTTP_PROXY` is a convention clients can ignore, not a boundary.
Use `isolated` for no-network workloads, or an execution plugin with a
mandatory network enforcement path. Do not expose the shared host network to
untrusted tenants on the assumption that the helper below protects it.

Nonempty `SANDBOX_EGRESS_DENIED_ADDRESSES` or
`SANDBOX_EGRESS_ALLOWED_LITERALS` make the built-in backend refuse to start
with `SANDBOX_EGRESS_POLICY_UNSUPPORTED`. They are reserved for enforcing
plugins. Health capabilities report `egress_address_filter: false`.

An allowlist matches by *name*, but whoever controls a permitted name's DNS —
or any label under a permitted wildcard — decides what it resolves to. Because
the control plane and the sandboxes share a worker container, a name that
resolves back to the host is a path from a restricted workspace into the
trusted side of the boundary.

`agent_sandbox.egress.EgressPolicy` judges a resolved address separately from
the name that produced it, and refuses:

- loopback, unspecified, link-local, and multicast addresses;
- cloud instance-metadata endpoints, including the ones outside link-local
  space (`100.100.100.200`, `168.63.129.16`, `192.0.0.192`, `fd00:ec2::254`);
- addresses discovered by resolving the worker's hostname at policy creation
  time. This is a cached heuristic, not enumeration of every interface; an
  enforcing plugin must refresh its actual host/interface deny set;
- anything in `SANDBOX_EGRESS_DENIED_ADDRESSES`.

IPv6 forms that embed an IPv4 address — IPv4-mapped, 6to4, and the well-known
NAT64 prefix — are judged by the IPv4 address they carry, so an IPv4 rule
cannot be escaped by rewriting the address. Network-specific NAT64 prefixes are
not decoded: RFC 6052 allows the IPv4 in several positions, so the layout
cannot be recognised from the address alone. On such a network, list the
prefix's translations of the ranges you deny.

Private ranges are not denied by default, because allow-listing an intranet
hostname is legitimate. List them in `SANDBOX_EGRESS_DENIED_ADDRESSES` when
allow-listed names must stay out of them. Addresses that reach this host
without being assigned to it — a 1:1-NAT public address, a router
port-forward, a container host-gateway alias — are not discoverable at
runtime and belong in the same list.

`SANDBOX_EGRESS_ALLOWED_LITERALS` exempts an address an operator listed on
purpose: allowing `127.0.0.1` for a local development service is an explicit
choice rather than an oversight.

A denial names the *class* of address, not the address itself, so reporting it
to an agent does not confirm the topology it just probed.

An enforcing plugin must dial the address it checked rather than re-resolving
the hostname, and must prevent raw-socket or direct-connection bypasses.

### File API boundary

File and directory operations pin each path component with directory file
descriptors and `O_NOFOLLOW`. Agent commands may rename entries while an API
call runs; no later privileged syscall reuses a resolved host pathname.
The workspace's ancestors must remain deployment-managed and inaccessible to
agents. The API refuses symlink traversal, including links within the same
workspace; agents can still use such links in their own commands. Listing
reports links, and delete/move operate on the final link itself.

Reads accept regular files only, open nonblocking to avoid FIFOs hanging the
manager, and bound both the pre-read size check and the actual read. Writes
are atomic replacements; ownership and permissions are applied to file
descriptors. Recursive removal requires the platform's symlink-safe,
descriptor-relative implementation.

At `basic` isolation, a process that escapes its execution process group
is not reliably reclaimed by process-group signals. Output collection is
deadline-bounded, but complete descendant containment requires PID namespace
isolation or a backend with execution-scoped cgroup supervision. A cgroup
namespace alone is not a CPU/memory quota or execution-scoped process reaper.

### Template snapshot boundary

Template builds pin the source directory and open every descendant relative
to directory descriptors without following links. Directory/file replacement
races cannot redirect manager reads into host paths. Symlinks, including
absolute virtualenv interpreter links, are stored as links, not dereferenced.
Special files are refused; regular-file identity and version are checked
around reads. Extraction is validated locally before object-store upload.

Snapshot work has configurable byte, entry, depth, and cooperative time
budgets. Build/attach hold an exclusive global execution lock, excluding
scoped/unscoped execution and File API calls. A separate lifecycle lock fences
creation, templates, and destruction; duplicate releases wait for the same
cleanup. Cancellation drains real background work before returning.

This is not an atomic filesystem snapshot. In particular, escaped descendants
at `basic` may still mutate their files, so fd confinement is essential even
with locks. A file changed during a read is rejected; these checks do not
promise cross-file consistency against non-cooperating writers.

## Levels and deployment policies are independent

All Levels share API path confinement, authorization, generation checks,
bounded snapshot work, and lifecycle correctness. Higher Levels add verified
namespace boundaries; they do not repair or waive baseline correctness.

Choose the Level and other policies for the workload:

| Workload | Deployment choice |
| --- | --- |
| Local development or trusted enterprise agents | Select a supported Level; host networking may be intentional. Strong resource enforcement is an optional operational policy. |
| Agents needing process privacy or reliable PID-namespace teardown | Require `standard` or above; keep network and resource policy choices explicit. |
| Hostile public tenants | A namespace Level alone, including `strict`, is insufficient. Require suitable enforced aggregate budgets/process supervision and a network boundary (no network or mandatory selective enforcement), or a stronger execution backend such as a VM. |

`/healthz` reports `resource_control` separately from `cgroup_namespace`:
the built-in backend advertises configured per-process/UID/file limits, but
`sandbox_cpu_quota`, `sandbox_memory_quota`, `sandbox_pid_quota`,
`sandbox_disk_quota`, and `execution_cgroup_supervision` are all `false`.
An enforcing plugin must report its actual controls, not infer them from
the Level's name. See [Hardening](HARDENING.md) for conditional enhancements.

## Selection

- `auto` probes `strict`, `standard`, and `basic`, then selects the strongest
  supported level.
- An explicit level probes all levels for diagnostics but starts only if the
  requested level itself succeeds.
- The selected level and network mode are included in the runtime profile hash,
  preventing workers with different boundaries from sharing a route.

## Outer container prerequisites

At minimum:

- Linux user namespaces enabled and not exhausted.
- Bubblewrap, `setpriv`, and `prlimit` executable and non-setuid.
- The container seccomp/AppArmor/SELinux policy permits the namespace and mount
  operations used by the selected level.
- A trusted manager process may run as root so it can assign distinct outer
  UIDs; agent commands never retain that identity.

Higher levels may require the outer runtime to permit PID/proc or cgroup
namespace creation. Prefer a targeted runtime policy over broad capabilities.

### Host settings that fail opaquely

Both of the following surface as a bare `Operation not permitted` from every
sandboxed command, with nothing naming the cause. Preflight reports them as
warnings at startup so a probe failure is explicable.

**`kernel.apparmor_restrict_unprivileged_userns`** — Ubuntu 24.04 and later
enable it by default. `unshare(CLONE_NEWUSER)` still succeeds, but the
resulting namespace has no capabilities, which both Bubblewrap and the
isolation probes need. Either grant `userns` to the relevant binaries through
an AppArmor profile, or:

```bash
sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0
```

**`CAP_SETFCAP` for a root caller** — since Linux 5.12, a user namespace may
map uid 0 only when its creator held `CAP_SETFCAP`. The *bounding set* is what
counts, because Bubblewrap is reached through `execve` and the kernel
recomputes a root caller's permitted set from it. The capability is in
Docker's default set, but `capsh --drop=cap_setfcap` and a tightened
`CapabilityBoundingSet=` remove it. A non-root caller is unaffected — prefer
one where there is the choice.

### Hosts that enforce AppArmor

Docker on a host that enforces AppArmor (Ubuntu, including GitHub's hosted
runners) confines every container with its `docker-default` profile, which
denies the `mount` Bubblewrap performs to make `/` a slave mount inside its new
mount namespace. The startup probe then fails at every level with
`bwrap: Failed to make / slave: Permission denied` and the service exits.
Docker Desktop and OrbStack have no AppArmor, so the quick start does not hit it
and the shipped Compose files do not carry the opt-in. On an AppArmor host add
it to the trusted manager container, next to the `seccomp` setting:

```yaml
# compose.override.yaml
services:
  agent-sandbox:
    security_opt:
      - apparmor=unconfined
```

This widens the trusted manager's confinement in the same way as
`seccomp=unconfined`; a platform that can load a narrower reviewed profile that
permits those mounts should prefer it. The repository's own CI does exactly the
override above (`.github/ci/compose.apparmor.yaml`) together with the sysctl
in the previous section.

### Docker and compatible runtimes

Docker's default seccomp profile blocks the nested namespace syscalls used by
Bubblewrap. The included Compose quick start therefore sets
`security_opt: [seccomp=unconfined]` on the trusted manager container. Agent
processes do not run directly in that manager boundary: they still receive
`no_new_privs`, an outer UID, resource limits, and the strongest Bubblewrap
profile that passes the runtime probe.

On OrbStack 2026 with Docker Engine 29.4.0, this setting alone enabled `basic`
without additional capabilities. `standard` and `strict` were unavailable because
Docker masks paths under `/proc` by default, so `bwrap` cannot mount a nested
private procfs; `auto` selected `basic` and reported both failures under
`probe_failures` in `/healthz`.

Adding `systempaths=unconfined` to the same container reached `strict`, with the
PID and cgroup namespaces both active:

```yaml
services:
  agent-sandbox:
    security_opt: !override
      - seccomp=unconfined
      - systempaths=unconfined
```

This is a real trade: it unmasks `/proc` paths for the trusted manager process,
which is a wider surface than the default. It does not grant capabilities or
privileged mode, and agent commands remain inside the negotiated Bubblewrap
profile. `!override` is required because Compose rejects a merged list that
repeats `seccomp=unconfined`.

Treat all of this as an example, not a portable capability table: probe every
deployment environment.

A production platform may use a reviewed custom seccomp profile that permits
the required namespace operations instead. Do not use `--privileged`, mount
the Docker socket, or add broad capabilities merely to force a higher level.

## Deliberate exclusions

- No Docker socket or host namespace mounts.
- No automatic `--privileged` recommendation.
- No writable system directories.
- No cross-tenant writable package caches.
- No claim that namespace isolation equals a microVM boundary.

For hostile internet-facing workloads, assess the separate resource, process,
and network enforcement requirements above. A microVM plugin is one stronger
backend option, not a prerequisite for every Level or enterprise deployment;
keep the same control-plane fencing and audit contracts across backends.
