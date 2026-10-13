"""Aggregate per-sandbox enforcement where the kernel offers no cgroup.

Every sandbox runs under its own outer UID, so the set of processes with that
real UID *is* the sandbox's process tree, however deeply it forks or
daemonizes. A thread samples ``/proc`` every ``interval`` seconds and, per
sandbox:

- **memory**: sums PSS (``smaps_rollup``; RSS where unavailable) over the
  UID's processes; above the limit every process of the UID is SIGKILLed and
  the running executions report ``SANDBOX_MEMORY_LIMIT_EXCEEDED``.
- **CPU**: a token bucket refilled at ``cpu_cores`` CPU-seconds per second;
  when it runs dry the UID's processes are SIGSTOPped until it refills, then
  SIGCONTed. The sandbox's long-run CPU use converges to the quota.
- **disk**: (all schemes) sums the allocated blocks of the sandbox root
  every ``disk_interval`` seconds; when usage is over the limit and still
  growing, the running executions are killed with
  ``SANDBOX_DISK_QUOTA_EXCEEDED``. Deleting files is always allowed.

This is supervision, not kernel enforcement: a burst can exceed the memory
limit for up to one interval before the kill (allocation rate × interval),
CPU is shaped at interval granularity, and a process that lives shorter than
an interval is not charged. ``/healthz`` says so.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

MEMORY = "SANDBOX_MEMORY_LIMIT_EXCEEDED"
DISK = "SANDBOX_DISK_QUOTA_EXCEEDED"


@dataclass
class _Watched:
    uid: int
    root: Path
    execs: set[str] = field(default_factory=set)
    verdicts: dict[str, str] = field(default_factory=dict)
    cpu_seen: dict[int, tuple[int, int]] = field(default_factory=dict)
    budget: float = 0.0
    stopped: set[int] = field(default_factory=set)
    disk_used: int = 0
    disk_checked: float = 0.0
    peak_memory: int = 0
    io_seen: dict[int, tuple[int, int]] = field(default_factory=dict)
    io_budget: list[float] = field(default_factory=lambda: [0.0, 0.0])


def processes_by_uid(proc: Path = Path("/proc")) -> dict[int, list[int]]:
    found: dict[int, list[int]] = {}
    for entry in os.scandir(proc):
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError, ValueError):
            uid = os.stat(entry.path).st_uid  # owner of /proc/<pid> is the euid
            # The real UID is what setpriv set and what RLIMIT_NPROC counts.
            with open(f"{entry.path}/status") as stream:
                for line in stream:
                    if line.startswith("Uid:"):
                        uid = int(line.split()[1])
                        break
            found.setdefault(uid, []).append(int(entry.name))
    return found


def process_memory(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/smaps_rollup") as stream:
            for line in stream:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/statm") as stream:
            return int(stream.read().split()[1]) * _PAGE
    except (OSError, ValueError, IndexError):
        return 0


def process_cpu_ticks(pid: int) -> tuple[int, int]:
    """(own utime+stime, reaped children's cutime+cstime) in clock ticks."""
    try:
        with open(f"/proc/{pid}/stat") as stream:
            fields = stream.read().rpartition(")")[2].split()
        return int(fields[11]) + int(fields[12]), int(fields[13]) + int(fields[14])
    except (OSError, ValueError, IndexError):
        return 0, 0


def process_io(pid: int) -> tuple[int, int]:
    """Bytes this process caused to be read from / written to storage."""
    read = write = 0
    try:
        with open(f"/proc/{pid}/io") as stream:
            for line in stream:
                if line.startswith("read_bytes:"):
                    read = int(line.split()[1])
                elif line.startswith("write_bytes:"):
                    write = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return read, write


def can_read_foreign_io(setpriv: str, uid: int) -> bool:
    """Whether this process may read /proc/<pid>/io of another UID.

    That needs ptrace read access, which root has only with CAP_SYS_PTRACE in
    a container (Docker drops it by default).
    """
    import subprocess

    try:
        child = subprocess.Popen(
            [
                setpriv,
                "--reuid",
                str(uid),
                "--regid",
                str(uid),
                "--clear-groups",
                "--",
                "sleep",
                "5",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        time.sleep(0.05)
        with open(f"/proc/{child.pid}/io") as stream:
            stream.read()
        return True
    except OSError:
        return False
    finally:
        child.kill()
        child.wait()


def disk_usage(root: Path, *, budget: int = 2_000_000) -> int:
    """Allocated bytes under ``root``, without following symlinks."""
    total = 0
    stack = [str(root)]
    seen = 0
    while stack and seen < budget:
        path = stack.pop()
        try:
            entries = os.scandir(path)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen += 1
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                total += info.st_blocks * 512
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
    return total


class SandboxSupervisor:
    def __init__(
        self,
        *,
        memory_bytes: int | None,
        cpu_cores: float | None,
        disk_bytes: int | None,
        interval: float = 0.1,
        disk_interval: float = 2.0,
        kill_uid: Callable[[int], None] | None = None,
        io_read_bps: int | None = None,
        io_write_bps: int | None = None,
    ) -> None:
        self.io_read_bps = io_read_bps
        self.io_write_bps = io_write_bps
        self.memory_bytes = memory_bytes
        self.cpu_cores = cpu_cores
        self.disk_bytes = disk_bytes
        self.interval = interval
        self.disk_interval = disk_interval
        self._kill_uid = kill_uid
        self._lock = threading.Lock()
        self._watched: dict[str, _Watched] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(
            self.memory_bytes
            or self.cpu_cores
            or self.disk_bytes
            or self.io_read_bps
            or self.io_write_bps
        )

    def start(self) -> None:
        if self._thread is None and self.enabled:
            self._thread = threading.Thread(
                target=self._run, name="sandbox-supervisor", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            for watched in self._watched.values():
                self._resume(watched)
        self._thread = None

    def begin(self, sandbox_id: str, uid: int, root: Path, exec_key: str) -> None:
        with self._lock:
            watched = self._watched.get(sandbox_id)
            if watched is None or watched.uid != uid:
                watched = _Watched(uid=uid, root=root)
                watched.budget = (self.cpu_cores or 0) * 1.0
                self._watched[sandbox_id] = watched
            watched.execs.add(exec_key)

    def end(self, sandbox_id: str, exec_key: str) -> str | None:
        with self._lock:
            watched = self._watched.get(sandbox_id)
            if watched is None:
                return None
            watched.execs.discard(exec_key)
            verdict = watched.verdicts.pop(exec_key, None)
            if not watched.execs:
                self._resume(watched)
                watched.cpu_seen.clear()
            return verdict

    def forget(self, sandbox_id: str) -> None:
        with self._lock:
            watched = self._watched.pop(sandbox_id, None)
            if watched is not None:
                self._resume(watched)

    def disk_exceeded(self, sandbox_id: str, root: Path, extra: int = 0) -> bool:
        """For File API writes: would ``extra`` more bytes exceed the quota?"""
        if not self.disk_bytes:
            return False
        return disk_usage(root) + extra > self.disk_bytes

    # ── the loop ──

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.wait(self.interval):
            now = time.monotonic()
            elapsed, last = now - last, now
            try:
                self._tick(elapsed, now)
            except Exception:  # never let the supervisor die
                logger.exception("sandbox supervisor tick failed")

    def _tick(self, elapsed: float, now: float) -> None:
        with self._lock:
            active = {sid: w for sid, w in self._watched.items() if w.execs}
        if not active:
            return
        need_procs = bool(
            self.memory_bytes or self.cpu_cores or self.io_read_bps or self.io_write_bps
        )
        by_uid = processes_by_uid() if need_procs else {}
        for sandbox_id, watched in active.items():
            pids = by_uid.get(watched.uid, [])
            if self.memory_bytes and pids:
                used = sum(process_memory(pid) for pid in pids)
                watched.peak_memory = max(watched.peak_memory, used)
                if used > self.memory_bytes:
                    logger.warning(
                        "sandbox %s uses %d bytes over its %d limit; killing",
                        sandbox_id,
                        used,
                        self.memory_bytes,
                    )
                    self._kill(watched, pids, MEMORY)
                    continue
            throttle = False
            if self.cpu_cores:
                throttle |= self._cpu_over(watched, pids, elapsed)
            if self.io_read_bps or self.io_write_bps:
                throttle |= self._io_over(watched, pids, elapsed)
            if self.cpu_cores or self.io_read_bps or self.io_write_bps:
                self._throttle(watched, pids, throttle)
            if self.disk_bytes and now - watched.disk_checked >= self.disk_interval:
                watched.disk_checked = now
                used = disk_usage(watched.root)
                growing = used > watched.disk_used
                watched.disk_used = used
                if used > self.disk_bytes and growing:
                    logger.warning("sandbox %s exceeds its disk quota; killing", sandbox_id)
                    self._kill(watched, pids or by_uid_for(watched.uid), DISK)

    def _io_over(self, watched: _Watched, pids: list[int], elapsed: float) -> bool:
        seen = {pid: process_io(pid) for pid in pids}
        used = [0, 0]
        for pid, current in seen.items():
            before = watched.io_seen.get(pid, (0, 0))
            used[0] += max(0, current[0] - before[0])
            used[1] += max(0, current[1] - before[1])
        watched.io_seen = seen
        over = False
        for index, rate in enumerate((self.io_read_bps, self.io_write_bps)):
            if rate:
                watched.io_budget[index] = min(
                    float(rate), watched.io_budget[index] + rate * elapsed - used[index]
                )
                over |= watched.io_budget[index] < 0
        return over

    def _throttle(self, watched: _Watched, pids: list[int], stop: bool) -> None:
        if stop:
            for pid in pids:
                if pid not in watched.stopped:
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGSTOP)
                        watched.stopped.add(pid)
        elif watched.stopped:
            self._resume(watched)

    def _cpu_over(self, watched: _Watched, pids: list[int], elapsed: float) -> bool:
        assert self.cpu_cores
        own = 0
        reaped = 0
        seen: dict[int, tuple[int, int]] = {}
        for pid in pids:
            current = process_cpu_ticks(pid)
            before = watched.cpu_seen.get(pid, (0, 0))
            seen[pid] = current
            own += max(0, current[0] - before[0])
            reaped += max(0, current[1] - before[1])
        # A reaped child's time reappears in its parent's cutime; only the part
        # not already charged while the child was alive is new.
        vanished = sum(a + b for pid, (a, b) in watched.cpu_seen.items() if pid not in seen)
        used = own + max(0, reaped - vanished)
        watched.cpu_seen = seen
        burst = self.cpu_cores * 1.0
        watched.budget = min(burst, watched.budget + self.cpu_cores * elapsed - used / _TICKS)
        return watched.budget < 0

    def _resume(self, watched: _Watched) -> None:
        for pid in watched.stopped:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGCONT)
        watched.stopped.clear()

    def _kill(self, watched: _Watched, pids: list[int], verdict: str) -> None:
        for key in watched.execs:
            watched.verdicts.setdefault(key, verdict)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        if self._kill_uid is not None:
            self._kill_uid(watched.uid)
        watched.stopped.clear()

    def report(self) -> dict[str, object]:
        return {
            "interval_seconds": self.interval,
            "disk_interval_seconds": self.disk_interval,
            "memory_metric": "sum of PSS (RSS where smaps_rollup is unreadable) over the sandbox UID's processes",
        }


def by_uid_for(uid: int) -> list[int]:
    return processes_by_uid().get(uid, [])
