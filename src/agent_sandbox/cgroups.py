"""Per-sandbox resource limits on a delegated cgroup v2 subtree.

The worker owns the cgroup it starts in. At startup it moves its own
processes into a ``worker`` leaf (cgroup v2 forbids processes in a cgroup
that delegates controllers), enables ``cpu``, ``memory`` and ``pids`` for
its children, and creates ``sandboxes``. Each sandbox gets a child of
``sandboxes`` carrying ``cpu.max``, ``memory.max``, ``memory.swap.max``,
``memory.oom.group`` and ``pids.max``; each execution runs in its own leaf
under the sandbox, so the sandbox limit is shared by every command in it and
one execution can be killed and accounted for on its own.

Nothing here needs privileges beyond a writable cgroup2 mount for the
worker's own cgroup, which Docker provides with ``--cgroupns=private`` and a
read-write ``/sys/fs/cgroup`` (see docs/ISOLATION.md).
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CONTROLLERS = ("cpu", "memory", "pids")
_CPU_PERIOD_US = 100_000
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


class CgroupUnavailable(RuntimeError):
    """The worker cannot manage a delegated cgroup v2 subtree."""


@dataclass(frozen=True, slots=True)
class CgroupLimits:
    cpu_cores: float | None
    memory_bytes: int | None
    pids: int | None
    worker_memory_reserve_bytes: int = 0


def _write(path: Path, value: str) -> None:
    with open(path, "w") as stream:
        stream.write(value)


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _own_cgroup(mount: Path) -> Path:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        hierarchy, _, rest = line.partition(":")
        controllers, _, relative = rest.partition(":")
        if hierarchy == "0" and controllers == "":
            path = mount / relative.lstrip("/")
            if path.is_dir():
                return path
            # A host-namespace path that is not visible here (no private
            # cgroupns): the mount root is then our own cgroup only if we
            # were started with a private namespace, so refuse.
            raise CgroupUnavailable(f"own cgroup {relative!r} is not visible under {mount}")
    raise CgroupUnavailable("no cgroup v2 entry in /proc/self/cgroup")


def _name(value: str) -> str:
    return _SAFE_NAME.sub("_", value)[:200] or "_"


@dataclass
class CgroupManager:
    """Owns ``<base>/sandboxes/<sandbox>/<exec>``. Threads may call it."""

    limits: CgroupLimits
    mount: Path = Path("/sys/fs/cgroup")
    base: Path | None = None
    active: bool = False
    detail: str = "not initialized"
    _created: set[Path] = field(default_factory=set)
    _helper: subprocess.Popen[bytes] | None = None
    helper_mount_point: Path = Path("/run/sandloom-cgroup")

    # ── setup ──

    def setup(self) -> None:
        try:
            self._setup()
            self.active = True
            self.detail = f"cgroup v2 at {self.base}"
        except (OSError, CgroupUnavailable) as exc:
            self.active = False
            self.detail = f"cgroup v2 delegation unavailable: {exc}"
            raise CgroupUnavailable(self.detail) from exc

    def _setup(self) -> None:
        if not (self.mount / "cgroup.controllers").exists():
            raise CgroupUnavailable(f"{self.mount} is not a cgroup2 mount")
        try:
            base: Path | None = _own_cgroup(self.mount)
        except CgroupUnavailable:
            base = None
        if (
            base is None
            or _read_only(self.mount)
            or not os.access(base / "cgroup.subtree_control", os.W_OK)
        ):
            # Docker and Kubernetes mount the container's cgroup read-only even
            # with a private cgroup namespace. A user namespace we create may
            # mount a fresh cgroup2 rooted at our own cgroup, read-write. The
            # helper process holds that mount; we use it through its /proc root.
            base = self._mount_through_helper()
        self._configure(base)

    def _mount_through_helper(self) -> Path:
        unshare = shutil.which("unshare")
        if unshare is None:
            raise CgroupUnavailable("cgroup is read-only and unshare(1) is not installed")
        self.helper_mount_point.mkdir(parents=True, exist_ok=True)
        helper = subprocess.Popen(
            [
                unshare,
                "--user",
                "--map-root-user",
                "--mount",
                "--cgroup",
                "--propagation",
                "private",
                "/bin/sh",
                "-c",
                'mount -t cgroup2 none "$0" && echo ready && exec sleep infinity',
                str(self.helper_mount_point),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert helper.stdout is not None
        line = helper.stdout.readline()
        if line.strip() != b"ready":
            helper.kill()
            _, err = helper.communicate(timeout=5)
            raise CgroupUnavailable(
                "cgroup is read-only and a user-namespace cgroup2 mount failed: "
                + err.decode(errors="replace").strip()[:300]
            )
        self._helper = helper
        base = Path(f"/proc/{helper.pid}/root") / self.helper_mount_point.relative_to("/")
        if not os.access(base / "cgroup.subtree_control", os.W_OK):
            raise CgroupUnavailable(f"helper cgroup mount {base} is not writable")
        return base

    def _configure(self, base: Path) -> None:
        available = set(_read(base / "cgroup.controllers").split())
        missing = [name for name in CONTROLLERS if name not in available]
        if missing:
            raise CgroupUnavailable(f"controllers {missing} not delegated to {base}")
        worker = base / "worker"
        worker.mkdir(exist_ok=True)
        # Move every process of our cgroup, not only this one: an entrypoint
        # shell or init may share it, and one leftover blocks the delegation.
        for _ in range(10):
            pids = _read(base / "cgroup.procs").split()
            if not pids:
                break
            for pid in pids:
                with contextlib.suppress(ProcessLookupError, OSError):
                    _write(worker / "cgroup.procs", pid)
        _write(base / "cgroup.subtree_control", " ".join(f"+{c}" for c in CONTROLLERS))
        sandboxes = base / "sandboxes"
        sandboxes.mkdir(exist_ok=True)
        _write(sandboxes / "cgroup.subtree_control", " ".join(f"+{c}" for c in CONTROLLERS))
        reserve = self.limits.worker_memory_reserve_bytes
        if reserve:
            # Protect the control process from reclaim, and keep the sandboxes
            # aggregate below the worker's own limit by the reserve, so a
            # sandbox OOM is local and the worker never reaches its limit.
            with contextlib.suppress(OSError):
                _write(worker / "memory.min", str(reserve))
            total = _effective_memory_max(base)
            if total is not None:
                _write(sandboxes / "memory.max", str(max(total - reserve, 64 * 1024 * 1024)))
        _write(sandboxes / "memory.oom.group", "0")
        self.base = base
        # Leftovers of a previous worker process in this container.
        for stale in sandboxes.iterdir():
            if stale.is_dir():
                self._kill_tree(stale)

    # ── per sandbox and per execution ──

    def _sandbox_dir(self, sandbox_id: str) -> Path:
        assert self.base is not None
        return self.base / "sandboxes" / _name(sandbox_id)

    def ensure_sandbox(self, sandbox_id: str) -> Path:
        path = self._sandbox_dir(sandbox_id)
        if not path.is_dir():
            path.mkdir(exist_ok=True)
            limits = self.limits
            if limits.cpu_cores:
                quota = max(1000, int(limits.cpu_cores * _CPU_PERIOD_US))
                _write(path / "cpu.max", f"{quota} {_CPU_PERIOD_US}")
            if limits.memory_bytes:
                _write(path / "memory.max", str(limits.memory_bytes))
                # Without this, a sandbox at its limit swaps instead of
                # failing, and drags the whole host down with it.
                with contextlib.suppress(OSError):
                    _write(path / "memory.swap.max", "0")
            if limits.pids:
                _write(path / "pids.max", str(limits.pids))
            _write(path / "cgroup.subtree_control", " ".join(f"+{c}" for c in CONTROLLERS))
        return path

    def create_execution(self, sandbox_id: str, exec_id: str) -> Path:
        parent = self.ensure_sandbox(sandbox_id)
        leaf = parent / f"exec-{_name(exec_id)}-{time.monotonic_ns()}"
        leaf.mkdir()
        # One OOM kill takes the whole command tree, not a random child that
        # leaves a half-dead build behind.
        with contextlib.suppress(OSError):
            _write(leaf / "memory.oom.group", "1")
        return leaf

    def execution_stats(self, leaf: Path) -> dict[str, int]:
        stats: dict[str, int] = {}
        for name in ("memory.events", "pids.events"):
            for line in _read(leaf / name).splitlines():
                key, _, value = line.partition(" ")
                with contextlib.suppress(ValueError):
                    stats[f"{name.split('.')[0]}.{key}"] = int(value)
        # A sandbox-level OOM (sum of executions over memory.max) is
        # recorded on the sandbox cgroup, which is the one at its limit.
        parent = leaf.parent
        for line in _read(parent / "memory.events.local").splitlines():
            key, _, value = line.partition(" ")
            if key == "oom_kill":
                with contextlib.suppress(ValueError):
                    stats["sandbox.oom_kill"] = int(value)
        return stats

    def remove_execution(self, leaf: Path) -> None:
        self._kill_tree(leaf)

    def remove_sandbox(self, sandbox_id: str) -> None:
        if self.active:
            self._kill_tree(self._sandbox_dir(sandbox_id))

    def sandbox_processes(self, sandbox_id: str) -> int:
        if not self.active:
            return 0
        path = self._sandbox_dir(sandbox_id)
        return int(_read(path / "pids.current") or 0) if path.is_dir() else 0

    def _kill_tree(self, path: Path) -> None:
        if not path.is_dir():
            return
        for child in [p for p in path.iterdir() if p.is_dir()]:
            self._kill_tree(child)
        with contextlib.suppress(OSError):
            _write(path / "cgroup.kill", "1")
        for _ in range(100):
            try:
                path.rmdir()
                return
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return
                if exc.errno != errno.EBUSY:
                    raise
                time.sleep(0.02)
        logger.warning("cgroup %s still busy after kill", path)

    def close(self) -> None:
        """Stop the mount helper; cgroups it created stay valid until removed."""
        helper, self._helper = self._helper, None
        if helper is not None:
            helper.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                helper.wait(timeout=5)

    def report(self) -> dict[str, object]:
        return {
            "active": self.active,
            "detail": self.detail,
            "path": str(self.base) if self.base else None,
        }


def _read_only(mount: Path) -> bool:
    try:
        return bool(os.statvfs(mount).f_flag & os.ST_RDONLY)
    except OSError:
        return True


def _effective_memory_max(path: Path) -> int | None:
    """The tightest memory.max from ``path`` up through visible ancestors."""
    limit: int | None = None
    current = path
    while (current / "memory.max").exists():
        value = _read(current / "memory.max")
        if value and value != "max":
            limit = int(value) if limit is None else min(limit, int(value))
        if current.parent == current:
            break
        current = current.parent
    if limit is None:
        try:
            limit = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError):
            return None
    return limit


def placement_prefix(leaf: Path) -> list[str]:
    """argv that joins ``leaf`` and then execs the rest of the command.

    The process enters the cgroup before prlimit/setpriv/bwrap run, so every
    descendant inherits it and none can escape by double-forking.
    """
    return ["/bin/sh", "-c", 'echo $$ > "$0" && exec "$@"', str(leaf / "cgroup.procs")]
