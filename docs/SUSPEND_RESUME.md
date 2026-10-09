# Suspend and resume

An agent that starts a long task and then waits (for CI, a human review, or an
upstream job) holds a sandbox slot the whole time. Suspend gives that slot
back and keeps the workspace. Resume takes a slot again and continues with the
same files. Nothing is frozen: suspend is refused while a command runs, and a
resumed sandbox starts with no processes. It is a lifecycle layer over the
workspace, not a process checkpoint.

```bash
# Give the slot back while waiting. Refused with 409 while anything runs.
POST /api/v1/sandboxes/my-agent/suspend   {"generation":3}

# Continue later. Idempotent; use the generation it returns from then on.
POST /api/v1/sandboxes/my-agent/resume
```

MCP exposes the same two operations as `sandbox_suspend(sandbox_id, generation)`
and `sandbox_resume(sandbox_id)`. Resolving a suspended sandbox through
`/api/v1/sandboxes/resolve` (or `sandbox_resolve`) also wakes it, so a client
that knows nothing about suspend keeps working.

## States

```text
READY ──suspend──▶ SUSPENDING ──worker released the slot──▶ SUSPENDED
  ▲                    │ refused (busy, snapshot failed)        │
  └────────────────────┘                                        │
  ▲                                                             │
  └── READY ◀── ASSIGNED ◀──────────────resume─────────────────┘
                   │ worker refused or unreachable: back to SUSPENDED
```

Every arrow is a compare-and-set on `(sandbox_id, generation, status)` in the
metadata store. There is no new table or column: `SUSPENDING` and `SUSPENDED`
are values of the existing route status.

| Call | In state | Result |
| --- | --- | --- |
| suspend | `READY` | `SUSPENDED`, slot released |
| suspend | `SUSPENDED` | 200 with `"suspended": false`; nothing changes |
| suspend | `RUNNING` | 409 `SANDBOX_SUSPEND_BUSY`; nothing changes |
| suspend | `ASSIGNED` (never created) | 409 `SANDBOX_NOT_READY` |
| suspend | stale generation | 409 `STALE_SANDBOX_GENERATION` |
| resume | `SUSPENDED` | `READY`, `"resumed": true` |
| resume | `ASSIGNED`/`READY`/`RUNNING` | 200 with `"resumed": false`; nothing changes |
| resume | `SUSPENDING` | 409 `SANDBOX_SUSPEND_IN_PROGRESS`; retry |
| resume | `RELEASED` (e.g. retention expired) | 409 `STALE_SANDBOX_ROUTE`; `resolve` starts a new, empty sandbox |
| exec, file call, connect | `SUSPENDING`/`SUSPENDED` | 409 `SANDBOX_SUSPENDED` |
| release | `SUSPENDED` | released, directory and snapshot deleted |

## Concurrency with exec

Suspend affects only the sandbox it names, and it never interrupts work in
that sandbox.

- **Admission and suspend decide on the same row.** Exec admission locks the
  route row and moves it to `RUNNING`; suspend claims it only from `READY` with
  no active execution. The admission write re-checks the status, so on SQLite
  (which has no `FOR UPDATE`) a suspend that committed first still wins. One of
  the two wins; the other gets a 409 that says why.
- **The worker re-checks before releasing the slot.** It takes the sandbox's
  exclusive lifecycle lock without waiting. Any command or file call still
  inside the sandbox (in any `exec_scope`) makes the suspend fail with
  `SANDBOX_SUSPEND_BUSY`, and the route goes back to `READY`. One agent thread
  suspending therefore can never kill another thread's command.
- **No torn state.** The sandbox leaves the worker's live set while that lock is
  held. A file call that obtained its handle earlier is refused with
  `SANDBOX_SUSPENDED` when it takes its own lock, so nothing writes into a
  dormant workspace or into a snapshot being taken.

## Where a resume lands

1. **The original worker, if it is live, on the current profile, and has a free
   slot.** The directory is reused in place. The generation is unchanged, so a
   client keeps using the one it had, unless the worker restarted (new epoch),
   in which case the generation advances like any reassignment.
2. **Any worker, for a shared workspace** (`SANDBOX_SHARED_ROOT`). The
   directory is already reachable; the generation advances.
3. **Another worker, from the snapshot**, for a local workspace whose suspend
   uploaded one. The tree is rebuilt in a staging directory, handed to the
   sandbox UID without following links, and renamed into place; the generation
   advances, which fences the old owner.
4. Otherwise the call fails and the sandbox stays `SUSPENDED`:
   `503 NO_SANDBOX_WORKER_AVAILABLE` when the original worker is live but full,
   `503 SANDBOX_DORMANT_WORKSPACE_UNAVAILABLE` when the only copy is on a worker
   that is not running. Both are retryable; nothing is deleted.

A resume that fails after it claimed the route (worker refused, restore failed,
network error) puts the route back to `SUSPENDED`, so the next attempt starts
over. Attached templates come back on the same worker; a sandbox that moved has
to attach them again.

## Snapshots

`SANDBOX_SUSPEND_SNAPSHOT` decides whether a suspend also archives the
workspace to the object store:

- `auto` (default): for local storage when an object store is configured.
- `always`: refuse to suspend (501 `SANDBOX_SNAPSHOT_UNAVAILABLE`) without one.
- `never`: the dormant workspace lives only on its worker.

A snapshot covers `/workspace`, `/home` and `/envs`; `/cache` and the logs are
recreated empty. It is written under `checkpoints/<sandbox_id>/dormant/` with
the same bounded, descriptor-relative archiver templates use, so the template
snapshot limits (`SANDBOX_TEMPLATE_MAX_EXTRACT_BYTES`,
`SANDBOX_TEMPLATE_MAX_ARCHIVE_BYTES`, `SANDBOX_TEMPLATE_SNAPSHOT_MAX_ENTRIES`,
`SANDBOX_TEMPLATE_SNAPSHOT_MAX_DEPTH` and
`SANDBOX_TEMPLATE_SNAPSHOT_TIMEOUT_SECONDS`) apply. A
manifest written last makes a snapshot visible only once complete, and parts
are verified against its digests before they are extracted. A workspace the
archiver refuses (a socket or FIFO, a tree past the limits) fails the suspend
with 409 `SANDBOX_SNAPSHOT_FAILED` and the sandbox stays `READY`. A shared
workspace is never snapshotted. The snapshot is deleted when the sandbox
resumes or is released.

## Retention and disk

- **Retention.** A suspended sandbox is kept for
  `SANDBOX_SUSPENDED_RETENTION_SECONDS` (default 604800, seven days) from the
  moment it was suspended. The maintenance cycle then releases it with reason
  `SUSPEND_EXPIRED`, which deletes its directory on the worker and its
  snapshot. `0` keeps suspended sandboxes until a client releases them.
  Suspended sandboxes are not subject to `SANDBOX_IDLE_TTL_SECONDS`, and
  nothing a suspended sandbox refuses resets the clock.
- **Disk pressure.** When the disk is over `SANDBOX_DISK_HIGH_WATERMARK_PERCENT`
  or under `SANDBOX_MIN_FREE_BYTES`, the maintenance cycle deletes the local
  copies of dormant sandboxes that have a snapshot, oldest first, until the
  disk recovers. They stay `SUSPENDED` and resume from the snapshot. A dormant
  sandbox without a snapshot is never evicted for disk; its directory is the
  only copy.
- **Stalled suspends.** A route left in `SUSPENDING` (for example, the worker
  was unreachable) for longer than `SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS` is
  finished by the next maintenance cycle.

`/healthz` and the admin overview report the retention period under
`reclamation.suspended_retention_seconds`.

## Limitations

- No process freezing and no memory restore. Background commands must have
  finished, and their results stay readable through the execution record.
- Disk eviction is tracked in the worker's memory. A worker that restarts
  forgets which dormant copies it evicted; their resume still restores from the
  snapshot because the directory is missing.
- A sandbox that resumed on another worker from its snapshot leaves its old
  directory on the original worker. Nothing deletes that stale copy when the
  original worker comes back; budget its disk or clean it out of band.
- With local storage and no snapshot, a worker that never comes back takes the
  dormant workspace with it, exactly as it would take a running one. Retention
  eventually releases the route.
- Restored files keep their modes, except that setuid/setgid bits and
  group/other write bits are dropped by the extractor.
- A resume racing another resume returns as soon as one of them has claimed the
  route; the loser may see `ASSIGNED` for the moment before the winner's worker
  marks it `READY`.
