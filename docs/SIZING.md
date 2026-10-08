# Sizing

Numbers here were measured with `scripts/load-test.py` on the host described
below. Absolute throughput will differ on your hardware; the *ratios* and the
method transfer, so re-run the script on the machine you intend to deploy to
rather than trusting these figures directly.

```bash
export SANDBOX_INTERNAL_TOKEN=...
./scripts/load-test.py --sandboxes 60 --execs 600 --concurrency 32 \
    --container "$(docker compose ps --format '{{.Name}}' agent-sandbox)"
```

The container name is the compose project's, which is the directory you checked
out into, so it is looked up rather than written out. `--container` names the
container whose memory and PID counts are sampled; a name that `docker stats`
does not know is refused before the first request instead of quietly producing a
report without those sections. Omit the flag to measure throughput only.

The run exits non-zero if any command failed, and prints the first few failures
with their ids and answers — a count on its own leaves you nothing to look up.
That is what found the SQLite write timeout: one command in six hundred answered
HTTP 500 with `database is locked`, which no amount of correctness testing would
have shown, because every one of them was a command that would have succeeded a
moment later.

## What was measured

14-core Apple Silicon, 24 GiB, Docker Desktop, unconstrained container
(no CPU or memory limit, no PID limit), isolation `basic`.

| Property | Measurement |
| --- | --- |
| Idle sandbox density | 50 sandboxes moved container memory by 0.1 MiB and added no PIDs |
| Sandbox create rate | ~24–31/s |
| Per-command fixed cost | **~49 ms** (`/bin/true`, serial p50) |
| Command latency model | `49 ms + command duration` (measured: `sleep 1` → 1091 ms) |
| Memory per concurrent command | ~2.5 MiB (106 MiB idle → ~185 MiB at 32 concurrent) |
| Throughput, SQLite | 24–31 cmd/s (25 typical) |
| Throughput, PostgreSQL | **35 cmd/s** |

Where a command's time actually goes is worth stating plainly. At 32-way
concurrency the mean command latency was ~1.4 s while the command itself runs
for 49 ms, so roughly **97% of that time is queueing in the control plane, not
executing**. The number that matters for sizing is not how fast a command runs
— that is whatever your command does — but how long it waits to start.

## The three numbers that drive capacity

**1. Idle sandboxes are nearly free.** Fifty of them moved container memory by
0.1 MiB and added no PIDs. An idle sandbox is a directory tree and one row in
the metadata store; nothing is resident until a command runs. Density is
therefore bounded by **disk**.

`SANDBOX_WORKER_CAPACITY` (default 32) is the admission threshold for placing
a new sandbox, and it is **not a hard limit**. The check compares against the
worker's last reported `running_sessions`, so a burst overshoots before the
next heartbeat corrects it — measured at 40 live sandboxes against a capacity
of 32. Treat it as a load-balancing target, and size the disk and the metadata
store for the overshoot rather than assuming the number is enforced exactly.

**2. Each command costs a fixed ~49 ms before the command runs.** Namespace
setup, the UID drop, and the metadata writes are paid per command, so a
thousand 20 ms commands cost far more than their work: total ≈
`n × (49 ms + duration)`. This is the design working as intended for an agent
runtime — favor fewer, longer commands over many tiny ones. A loop that shells
out per file will spend almost all of its time in this constant.

**3. The metadata backend, not the CPU, is the ceiling.** At 35 cmd/s the
process used roughly 1.7 of 14 cores. Throughput stayed flat, between 24 and
31 cmd/s, as offered concurrency went from 16 to 128, while latency grew with
it — the signature of a serialized writer, not a saturated CPU. Re-measured on
the host above at 60 sandboxes and 600 commands:

| Offered concurrency | Throughput | p50 | mean |
| --- | --- | --- | --- |
| 16 | 26 cmd/s | 302 ms | 618 ms |
| 32 | 31 cmd/s | 789 ms | 1040 ms |
| 64 | 24 cmd/s | 2366 ms | 2643 ms |
| 128 | 25 cmd/s | 4891 ms | 4949 ms |

A sixteen-fold increase in offered concurrency buys no throughput and costs
sixteen times the median wait.

That writer is SQLite, which the quick start uses by default and which allows
one writer at a time. Every command performs two metadata writes, so they
queue. Measured at an identical fleet size (60 sandboxes, 600 commands,
concurrency 32):

| Backend | Throughput | p50 | p95 | p99 |
| --- | --- | --- | --- | --- |
| SQLite | 25 cmd/s | 964 ms | 2875 ms | 4275 ms |
| PostgreSQL | 35 cmd/s | 890 ms | 1319 ms | **1504 ms** |

Same hardware, same load, one worker each; both rows re-measured here, SQLite
after it was put into WAL mode with a 15-second write timeout — which is why it
moved from 22 cmd/s to 25 and why the gap is now 1.4× rather than 1.6×.
Throughput is the smaller half of the difference: at p99 PostgreSQL is still
**2.8× quicker**, and the tail is what an agent actually waits on. SQLite's
single writer also means its latency grows with offered concurrency while
PostgreSQL's does not, which is the difference between a workload that degrades
and one that queues.

**SQLite is the right default for a single-replica quick start and the wrong
choice under concurrent load.** If you are sizing for real use, deploy
PostgreSQL or MySQL. The database was never the bottleneck in wall-clock terms
until commands got short and numerous; at that point it is the only bottleneck
that matters.

## Sizing guidance

| Resource | Guidance |
| --- | --- |
| **Memory** | ~2.5 MiB per concurrently running command, plus the base process (~100 MiB). 32 concurrent commands fit comfortably in 256 MiB. Idle sandboxes add nothing. |
| **CPU** | Not the limit for short commands. Budget by *command-seconds*: at 14 cores this host sustained 35 cmd/s at 49 ms each. Long CPU-bound commands are the case where core count binds. |
| **PID limit** | Proportional to *concurrently executing* commands, not to sandbox count. A container cgroup reported 16 PIDs with ~40 live sandboxes and no commands running, and only a handful of processes were visible inside `/proc` with 32 commands in flight — short commands spend nearly all their time queued rather than running. Long-running commands are the case that stresses it. |
| **Disk** | The real density bound. Each sandbox holds its workspace, home, and caches; a template-heavy fleet adds the shared read-only template cache, counted once per worker rather than per sandbox. |
| **Metadata backend** | PostgreSQL or MySQL for anything beyond a single replica. See the measurement above. |

## What this does not cover

These figures are for command throughput and density. They say nothing about
whether `basic` isolation is appropriate for your threat model — that is a
separate question, answered in [ISOLATION.md](ISOLATION.md) — and nothing about
multi-replica behaviour, which the metadata backend and registry choice
dominate.
