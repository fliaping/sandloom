# Hardening status and next milestones

This is a code-review backlog, not an independent security certification.
The project remains beta. Common correctness requirements apply at every
Level; additional deployment controls depend on the workload's threat model.
Passing namespace tests is not evidence that a shared worker is suitable for
hostile public multi-tenancy, but hostile-tenancy controls are not mandatory
prerequisites for ordinary open-source use or trusted enterprise agents.

## Completed in this hardening pass

- The File API uses descriptor-relative directory traversal with no-follow
  opens, bounded regular-file reads, and atomic writes. Deterministic tests
  replace directories during operations and check that host files remain
  untouched. Following even an in-workspace symlink is intentionally refused;
  listing, deleting, and moving a final symlink remain supported.
- File-path locks use canonical virtual paths, so alternate spellings cannot
  bypass the same lock.
- Execution deadlines include output drainage. A command leader exiting does
  not allow a descendant holding its output pipes to hang the API. Cleanup
  escalates to the remaining process group and bounds output-reader shutdown.
- The built-in backend rejects nonempty address-policy settings it cannot
  enforce. Its health report explicitly advertises no address filter.
- Partially failed application startup closes the service, registry, and
  metadata store. A failing close does not skip the other cleanup callbacks.
- Malformed non-ASCII authorization headers produce an authentication error
  instead of an internal server error.
- SBOM and distribution-pair verification use the project's interpreter rather
  than an unsupported system Python.
- Template snapshots use descriptor-relative no-follow traversal, preserve
  symlinks without reading their targets, reject special/changing files, and
  bound source bytes, compressed output, entries, depth, and cooperative time.
  Invalid archives are rejected locally before any object-store upload.
- Template build/attach exclude execution and File API work. Lifecycle locks
  fence creation and release; duplicate releases wait for cleanup. Cancelled
  thread-backed requests drain actual work before propagating cancellation.
- Health reports resource enforcement independently of the namespace Level,
  with aggregate sandbox quotas and cgroup supervision explicitly false for
  the built-in backend.

The changes do not add enterprise implementations or change plugin protocol
signatures. Intentional compatibility changes include File API/template source
symlink-traversal refusal, fail-closed built-in egress settings, conflict lock
errors for template operations, and adjustable snapshot work budgets; see
[Isolation](ISOLATION.md) and [Configuration](CONFIGURATION.md).

## Conditional enhancements: before accepting hostile tenants

These are gates for that deployment scenario, not universal release blockers
and not prerequisites for choosing `basic` or `standard`. Keep enforcing
policies pluggable; private enterprise implementations stay outside the core.

### Enforced resource budgets and process ownership

The built-in backend supplies process/file limits and namespace boundaries,
but a cgroup namespace is not a per-sandbox CPU, memory, or aggregate PID
quota. At `basic`, descendants can also escape a process group by starting a
new session. A bounded API response does not prove resource reclamation.

Add a supervised execution backend using delegated cgroup v2 resource control
or a VM boundary. Keep per-sandbox budgets distinct from per-execution groups
so cancelling one command does not terminate unrelated commands in that
sandbox. Probe the actual available controls and fail closed when a requested
budget cannot be enforced; do not infer them from the isolation-level label.

Acceptance: memory exhaustion, CPU saturation, fork storms, and session-escaped
descendants neither exhaust a neighboring tenant's budget nor survive the
applicable cancel, timeout, release, or worker-shutdown boundary. Also test
worker crashes and restart reconciliation. Apply a separate aggregate disk
quota; per-file size limits alone do not bound workspace usage.

### Network enforcement when selective access is required

`host` networking permits direct connections; proxy environment variables and
`EgressPolicy` alone cannot prevent bypasses. The current no-network option is
`isolated`. Selectively networked hostile workloads need an enforcing backend.

Acceptance: raw/direct TCP and UDP cannot bypass the enforcement path; checked
destinations are the ones actually dialed; DNS rebinding, alternate IPv6
representations, worker/control-plane addresses, and metadata endpoints are
denied as configured. Keep enforcement capability reporting independent of
the `basic`/`standard`/`strict` namespace labels.

## Remaining general robustness and compatibility work

- **Bound File API work, not just responses.** Async runtime methods currently
  execute filesystem work synchronously; a directory listing collects and
  sorts every name before paging. Add scan/entry/time budgets and move slow
  work off the event loop. Keep locks held until background work has actually
  ended, including after request cancellation. Acceptance includes a huge
  directory and recursive deletion running while health and unrelated tenant
  requests stay responsive.
- **Exercise independently packaged plugins.** The loaders and contracts are
  covered by core tests, but a real external plugin wheel needs its own
  compatibility matrix. Test all six factory types, startup failure, shutdown,
  missing/duplicate entry points, and rolling core upgrades. Version any
  incompatible protocol change. Enterprise schema/environment translations
  stay in the private package as described in [Private adapters](PRIVATE_ADAPTERS.md).
- **Deployment policy matrix.** Validate the negotiated boundaries on each
  supported Docker/Kubernetes policy, not only a broadly permitted CI
  container. CI currently adds `SYS_ADMIN` for isolation tests; success there
  does not certify the narrower production profile. Keep broad capabilities
  out of the default deployment and record per-profile probe evidence.
- **Repeat operational integration and load tests.** Run the advertised SQL,
  registry, object-storage, toolchain, and fleet scenarios against the target
  versions, then size capacity from measured CPU, memory, PID, disk, and API
  latency under both normal and adversarial load.

## Release gates remain separate

Complete the [open-source release checklist](OPEN_SOURCE_RELEASE.md), including
credential rotation, a clean public history, owned distribution names,
artifact/image scanning, and signing. A Git worktree that shares the original
repository's object database is not a clean public repository.

`build-public-mirror.sh` exports committed `HEAD` only. Review and commit the
intended public changes before using its `--verify --packages` gates; a passing
working-tree test run does not certify an exported commit. Do not copy private
adapter packages or deployment credentials into that mirror.
