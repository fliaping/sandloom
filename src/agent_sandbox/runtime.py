"""Isolated UID + prlimit + Bubblewrap execution layer."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import fcntl
import hashlib
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import agent_sandbox_runtime
from agent_sandbox_runtime import (
    PLATFORM_PYTHON_RUNTIME,
    PLATFORM_RUNTIME_PACKAGES,
    InteractiveEnvironment,
    InteractiveSandboxCommandBuilder,
    IsolationLevel,
    SandboxMount,
    SandboxRuntimeProfile,
    Toolchain,
    build_toolchain,
    negotiate_isolation,
    toolchains_needing_procfs,
    toolchains_unavailable_at,
)
from agent_sandbox_runtime import (
    BubblewrapCommandBuilder as SharedBubblewrapCommandBuilder,
)

from .archive import ArchiveLimits
from .config import Settings
from .dormant import restore_snapshot, write_snapshot
from .isolation_policy import negotiate_features
from .schemas import ExecRequest, ExecResponse
from .templates import (
    TEMPLATE_MOUNT_ROOT,
    LocalTemplateCache,
    TemplateManager,
    TemplateRecord,
    template_object_store,
)
from .threading import complete_before_cancelling, complete_in_thread
from .toolchain_probe import probe_toolchains
from .workspace import WorkspaceFiles, open_directory, workspace_parts

logger = logging.getLogger(__name__)

_GENERIC_RUNTIME_PROBE = """
import os
import sys

try:
    import agent_sandbox_runtime
    import sqlalchemy
except Exception as exc:
    print(f"runtime_unavailable:{type(exc).__name__}", file=sys.stderr)
    raise
"""


@dataclass(frozen=True, slots=True)
class LocalSandbox:
    sandbox_id: str
    generation: int
    uid: int
    root: Path

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"


@dataclass(slots=True)
class DormantSandbox:
    """A suspended sandbox: its directory is kept, its capacity slot is not.

    Only what a resume on this worker needs. `snapshot` records whether the
    object store holds a copy, which is what makes the local directory safe to
    evict under disk pressure.
    """

    sandbox_id: str
    generation: int
    uid: int
    suspended_at: float
    snapshot: bool
    templates: tuple[TemplateRecord, ...] = ()
    evicted: bool = False


@dataclass(frozen=True, slots=True)
class RuntimeExtension:
    """Generic customization supplied by an optional execution plugin.

    Private packages may inject deployment-managed environment variables,
    read-only mounts, directory targets, and an additional startup probe while
    reusing the public Bubblewrap backend.
    """

    managed_environment: tuple[tuple[str, str], ...] = ()
    mounts: tuple[SandboxMount, ...] = ()
    directories: tuple[str, ...] = ()
    probe_script: str = ""
    capability_labels: Mapping[str, object] = field(default_factory=dict)


class BubblewrapCommandBuilder:
    """Service adapter over the reusable Bubblewrap runtime package."""

    def __init__(
        self,
        settings: Settings,
        *,
        disable_nested_userns: bool = True,
        isolation_level: IsolationLevel = IsolationLevel.BASIC,
        additional_features: tuple[str, ...] = (),
        extension: RuntimeExtension | None = None,
    ) -> None:
        self.settings = settings
        self.disable_nested_userns = disable_nested_userns
        self.isolation_level = isolation_level
        self.extension = extension or RuntimeExtension()
        profile = _runtime_profile(
            settings,
            disable_nested_userns=disable_nested_userns,
            isolation_level=isolation_level,
            additional_features=additional_features,
        )
        self.profile = profile
        self._shared = InteractiveSandboxCommandBuilder(
            profile,
            InteractiveEnvironment(
                python_index_url=settings.python_index_url,
                npm_registry=settings.npm_registry,
                no_proxy=settings.no_proxy,
                http_proxies=tuple(settings.http_proxies),
                managed_environment=self.extension.managed_environment,
                platform_python_runtime=Path(sys.prefix),
                platform_runtime_packages=_platform_runtime_packages(),
                extra_mounts=self.extension.mounts,
                extra_directories=self.extension.directories,
                toolchains=configured_toolchains(settings),
            ),
        )
        self._probe = SharedBubblewrapCommandBuilder(profile)

    def build(
        self,
        sandbox: LocalSandbox,
        request: ExecRequest,
        *,
        template_mounts: Sequence[SandboxMount] = (),
    ) -> list[str]:
        # A login shell re-reads /etc/profile, which assigns PATH outright and
        # would hide every toolchain the sandbox was given. The profile written
        # here is what the shell reads afterwards.
        self._shared.ensure_login_path(sandbox_root=sandbox.root, sandbox_uid=sandbox.uid)
        return self._shared.build(
            sandbox_root=sandbox.root,
            sandbox_uid=sandbox.uid,
            argv=request.argv,
            cwd=request.cwd,
            user_env=request.env,
            extra_mounts=template_mounts,
        )

    @property
    def toolchains(self) -> tuple[Toolchain, ...]:
        """The languages this worker gives every sandbox."""
        return self._shared.environment.resolved_toolchains()

    def probe(self) -> dict[str, object]:
        return self._probe.probe()


def _platform_runtime_packages() -> Path:
    package_file = agent_sandbox_runtime.__file__
    if package_file is None:
        raise RuntimeError("cannot locate the managed agent_sandbox_runtime package")
    return Path(package_file).resolve().parent.parent


def configured_toolchains(settings: Settings) -> tuple[Toolchain, ...]:
    """Build the toolchains this deployment enabled, in configured order.

    Every registry is passed to every builder; each one takes only the keywords
    it declares. The Python toolchain additionally receives the platform
    interpreter paths, which the service mounts read-only so a sandbox can run
    Python without one installed in its own workspace.
    """

    options = {
        "index_url": settings.python_index_url,
        "registry": settings.npm_registry,
        "proxy": settings.go_proxy,
        "sumdb": settings.go_sumdb,
        "registry_url": settings.cargo_registry_url or None,
        "maven_repository_url": settings.maven_repository_url or None,
        "java_home": settings.java_home or None,
        "launcher_dir": settings.rust_launcher_dir or None,
        "platform_python_runtime": PLATFORM_PYTHON_RUNTIME,
        "platform_runtime_packages": PLATFORM_RUNTIME_PACKAGES,
    }
    return tuple(build_toolchain(name, **options) for name in settings.toolchains)


class SandboxRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        extension: RuntimeExtension | None = None,
        templates: TemplateManager | None = None,
    ) -> None:
        self.settings = settings
        self.extension = extension or RuntimeExtension()
        if settings.egress_denied_addresses or settings.egress_allowed_literals:
            raise ValueError(
                "SANDBOX_EGRESS_POLICY_UNSUPPORTED: the built-in execution backend "
                "cannot enforce address policies; use an enforcing execution plugin"
            )
        self.builder = BubblewrapCommandBuilder(settings, extension=self.extension)
        self.templates = templates or TemplateManager(
            LocalTemplateCache(
                settings.template_cache_root,
                max_total_bytes=settings.template_cache_max_bytes,
                max_extract_bytes=settings.template_max_extract_bytes,
            ),
            # The same store the catalog uses, or a template resolves everywhere
            # and its bytes live only here.
            object_store=template_object_store(settings),
            max_archive_bytes=settings.template_max_archive_bytes,
            snapshot_limits=ArchiveLimits(
                max_source_bytes=settings.template_max_extract_bytes,
                max_archive_bytes=settings.template_max_archive_bytes,
                max_entries=settings.template_snapshot_max_entries,
                max_depth=settings.template_snapshot_max_depth,
                timeout_seconds=settings.template_snapshot_timeout_seconds,
            ),
        )
        self.sandboxes: dict[str, LocalSandbox] = {}
        self.processes: dict[tuple[str, str], asyncio.subprocess.Process] = {}
        self.last_active_at: dict[str, float] = {}
        self._attached_templates: dict[str, tuple[TemplateRecord, ...]] = {}
        self._destroying: set[str] = set()
        self._destroy_tasks: dict[str, asyncio.Task[None]] = {}
        # Suspended sandboxes. Not in `sandboxes`, so they hold no capacity slot.
        self.dormant: dict[str, DormantSandbox] = {}

    async def probe(self) -> dict[str, object]:
        runtime_report = await asyncio.to_thread(self.builder.probe)
        probe_root = self.settings.workspace_root / f".probe-{uuid.uuid4().hex}"
        probe_id = probe_root.name
        try:
            for name in ("workspace", "home", "cache", "envs"):
                (probe_root / name).mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                _initialize_top_level,
                probe_root,
                self.settings.uid_start,
                ("workspace", "home", "cache", "envs"),
            )
            request = ExecRequest(
                exec_id="probe",
                generation=1,
                argv=[
                    f"{PLATFORM_PYTHON_RUNTIME}/bin/python",
                    "-c",
                    _GENERIC_RUNTIME_PROBE + self.extension.probe_script,
                ],
                timeout_seconds=10,
            )
            candidates: dict[IsolationLevel, BubblewrapCommandBuilder] = {}
            results: dict[IsolationLevel, tuple[bool, str]] = {}
            for level in reversed(tuple(IsolationLevel)):
                failures: list[str] = []
                for disable_nested_userns in (True, False):
                    candidate = BubblewrapCommandBuilder(
                        self.settings,
                        disable_nested_userns=disable_nested_userns,
                        isolation_level=level,
                        extension=self.extension,
                    )
                    ok, detail = await self._probe_candidate(
                        candidate,
                        LocalSandbox("probe", 1, self.settings.uid_start, probe_root),
                        request,
                    )
                    if ok:
                        candidates[level] = candidate
                        results[level] = (True, "ok")
                        break
                    failures.append(f"disable_nested_userns={disable_nested_userns}: {detail}")
                else:
                    results[level] = (False, " | ".join(failures))

            selection = negotiate_isolation(
                self.settings.isolation_level,
                lambda level: results[level],
            )
            self.builder = candidates[selection.selected]
            feature_candidates: dict[tuple[str, ...], BubblewrapCommandBuilder] = {
                (): self.builder
            }

            async def probe_features(features: tuple[str, ...]) -> tuple[bool, str]:
                failures = []
                for disable_nested in (True, False):
                    candidate = BubblewrapCommandBuilder(
                        self.settings,
                        isolation_level=selection.selected,
                        additional_features=features,
                        disable_nested_userns=disable_nested,
                        extension=self.extension,
                    )
                    ok, detail = await self._probe_candidate(
                        candidate,
                        LocalSandbox("probe", 1, self.settings.uid_start, probe_root),
                        request,
                    )
                    if ok:
                        feature_candidates[features] = candidate
                        return True, "ok"
                    failures.append(f"disable_nested_userns={disable_nested}: {detail}")
                return False, " | ".join(failures)

            feature_selection = await negotiate_features(
                selection.selected.features,
                self.settings.isolation_required_features,
                self.settings.isolation_optional_features,
                probe_features,
            )
            self.builder = feature_candidates[feature_selection.enabled]
            actual_features = self.builder.profile.features
            effective_hash = self.builder.profile.effective_profile_hash
            object.__setattr__(self.settings, "profile_hash", effective_hash)
            sandbox = LocalSandbox(probe_id, 1, self.settings.uid_start, probe_root)
            self.sandboxes[probe_id] = sandbox

            async def launch(request: ExecRequest) -> ExecResponse:
                return await self.execute(sandbox, request)

            checks = await probe_toolchains(
                (toolchain.name for toolchain in self.builder.toolchains), launch
            )
        finally:
            self.sandboxes.pop(probe_id, None)
            self.last_active_at.pop(probe_id, None)
            await asyncio.to_thread(shutil.rmtree, probe_root, True)
        toolchains = self.builder.toolchains
        return {
            **runtime_report,
            **selection.as_dict(),
            "features": list(actual_features),
            "profile_mode": "custom" if feature_selection.enabled else "level",
            "feature_policy": {
                "required": self.settings.isolation_required_features,
                "optional": self.settings.isolation_optional_features,
                "enabled_additions": list(feature_selection.enabled),
                "skipped_optional": feature_selection.skipped,
            },
            # Keep static hints for older clients, but report measured launch
            # results separately so a custom image can work at a lower Level.
            "toolchains": {
                "configured": [toolchain.name for toolchain in toolchains],
                "needs_procfs": list(toolchains_needing_procfs(toolchains)),
                "unavailable_at_this_level": list(
                    name
                    for name in toolchains_unavailable_at(actual_features, toolchains)
                    if checks.get(name, {}).get("status") in {"failed", "timed_out"}
                ),
                "isolation_hints": list(
                    toolchains_unavailable_at(actual_features, toolchains)
                ),
                "checks": checks,
                "available": [
                    name for name, check in checks.items() if check["status"] == "available"
                ],
            },
            "profile_hash": effective_hash,
            "private_tmp": True,
            "disable_nested_userns": self.builder.disable_nested_userns,
            # Verified in the target container: unprivileged OverlayFS is unavailable,
            # so the workspace always uses a plain bind mount.
            "overlay": False,
            "pid_namespace": "pid_namespace" in actual_features,
            "cgroup_namespace": "cgroup_namespace" in actual_features,
            "network_namespace": self.settings.network_mode == "isolated",
            "egress_address_filter": False,
            "resource_control": {
                "process_limits": {
                    "cpu_seconds_per_process": self.settings.cpu_seconds,
                    "processes_per_real_uid": self.settings.max_processes,
                    "open_files_per_process": self.settings.max_open_files,
                    "bytes_per_file": self.settings.max_file_size_bytes,
                },
                "sandbox_cpu_quota": False,
                "sandbox_memory_quota": False,
                "sandbox_pid_quota": False,
                "sandbox_disk_quota": False,
                "execution_cgroup_supervision": False,
            },
            "landlock": False,
            "extension": dict(self.extension.capability_labels),
        }

    async def _probe_candidate(
        self,
        builder: BubblewrapCommandBuilder,
        sandbox: LocalSandbox,
        request: ExecRequest,
    ) -> tuple[bool, str]:
        command = builder.build(sandbox, request)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError as exc:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            return False, f"{type(exc).__name__}: {exc}"
        except OSError as exc:
            return False, f"{type(exc).__name__}: {exc}"
        if process.returncode == 0:
            return True, "ok"
        return False, stderr.decode(errors="replace")[-2000:].strip() or "probe failed"

    async def create(self, sandbox_id: str, generation: int, uid: int) -> LocalSandbox:
        return await complete_in_thread(self._create_sandbox, sandbox_id, generation, uid)

    def _create_sandbox(self, sandbox_id: str, generation: int, uid: int) -> LocalSandbox:
        self._check_not_destroying(sandbox_id)
        with self._template_lock(sandbox_id):
            return self._create_sandbox_locked(sandbox_id, generation, uid)

    def _create_sandbox_locked(self, sandbox_id: str, generation: int, uid: int) -> LocalSandbox:
        self._check_not_destroying(sandbox_id)
        existing = self.sandboxes.get(sandbox_id)
        if existing and existing.generation == generation and existing.uid == uid:
            self.touch(sandbox_id)
            return existing
        root = _sandbox_root(self.settings.workspace_root, sandbox_id)
        root_existed = root.exists()
        if not root_existed and not self.disk_status()["available"]:
            raise RuntimeError("SANDBOX_WORKER_DISK_PRESSURE")
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise RuntimeError("the sandbox root may not be a symlink")
        names = ("workspace", "home", "cache", "envs", "logs")
        if root_existed:
            _validate_top_level(root, uid, names)
        else:
            for name in names:
                (root / name).mkdir(exist_ok=True)
            _initialize_top_level(root, uid, names)
        sandbox = LocalSandbox(sandbox_id=sandbox_id, generation=generation, uid=uid, root=root)
        self.sandboxes[sandbox_id] = sandbox
        self.touch(sandbox_id)
        return sandbox

    def get(self, sandbox_id: str, generation: int) -> LocalSandbox:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None or sandbox.generation != generation:
            raise RuntimeError("STALE_SANDBOX_GENERATION")
        return sandbox

    async def execute(self, sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        self.get(sandbox.sandbox_id, sandbox.generation)
        with self._execution_lock(sandbox, request.exec_scope):
            return await self._execute_locked(sandbox, request)

    @contextlib.contextmanager
    def _execution_lock(self, sandbox: LocalSandbox, exec_scope: str | None) -> Iterator[None]:
        """Lock the smallest reliable unit of work.

        A scoped execution takes a shared global lock plus an exclusive lock on
        its own scope, so different scopes run concurrently. An unscoped
        lifecycle command takes the global lock exclusively. Acquisition is
        nonblocking: conflicting work is refused rather than queued. Release
        separately drains this lock after terminating active executions.
        """
        self._check_not_destroying(sandbox.sandbox_id)
        lock_root = sandbox.root / ".exec-locks"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        global_stream = (lock_root / "global.lock").open("a+b")
        scope_stream = None
        try:
            global_mode = fcntl.LOCK_SH if exec_scope else fcntl.LOCK_EX
            fcntl.flock(global_stream.fileno(), global_mode | fcntl.LOCK_NB)
            if exec_scope:
                scope_stream = (lock_root / f"scope-{exec_scope}.lock").open("a+b")
                fcntl.flock(scope_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError as exc:
            raise RuntimeError(
                "SANDBOX_EXEC_SCOPE_LOCKED" if exec_scope else "SANDBOX_SHARED_WORKSPACE_LOCKED"
            ) from exc
        finally:
            if scope_stream is not None:
                fcntl.flock(scope_stream.fileno(), fcntl.LOCK_UN)
                scope_stream.close()
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_UN)
            global_stream.close()

    @contextlib.contextmanager
    def _path_lock(
        self, sandbox: LocalSandbox, path: str, *, shared: bool = False
    ) -> Iterator[None]:
        """Lock one target path so file calls do not serialize the whole sandbox."""
        self._check_not_destroying(sandbox.sandbox_id)
        exec_lock_root = sandbox.root / ".exec-locks"
        exec_lock_root.mkdir(mode=0o700, exist_ok=True)
        global_stream = (exec_lock_root / "global.lock").open("a+b")
        lock_root = sandbox.root / ".file-locks"
        lock_root.mkdir(mode=0o700, exist_ok=True)
        # A path can exceed filename limits and contain separators, so key the
        # lock file on a digest instead of the path itself.
        canonical = "/workspace/" + "/".join(workspace_parts(path))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        stream = (lock_root / f"path-{digest}.lock").open("a+b")
        try:
            # File calls may run in parallel, but must not bypass a lifecycle
            # command such as recovery or release that holds the global lock.
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(
                stream.fileno(),
                (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB,
            )
            # A suspend unregisters the sandbox while it holds the global lock
            # exclusively, so a call that obtained its handle earlier finds out
            # here instead of writing into a dormant workspace.
            self._check_not_dormant(sandbox)
            yield
        except BlockingIOError as exc:
            raise RuntimeError("SANDBOX_FILE_PATH_LOCKED") from exc
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()
            fcntl.flock(global_stream.fileno(), fcntl.LOCK_UN)
            global_stream.close()

    async def _execute_locked(self, sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse:
        self.touch(sandbox.sandbox_id)
        template_mounts = await asyncio.to_thread(self._resolve_templates, sandbox)
        self._check_not_destroying(sandbox.sandbox_id)
        self.get(sandbox.sandbox_id, sandbox.generation)
        command = self.builder.build(sandbox, request, template_mounts=template_mounts)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.processes[(sandbox.sandbox_id, request.exec_id)] = process
        timeout = request.timeout_seconds or self.settings.default_timeout_seconds
        status = "SUCCEEDED"
        assert process.stdout is not None and process.stderr is not None
        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, self.settings.max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, self.settings.max_output_bytes)
        )
        completion = asyncio.gather(process.wait(), stdout_task, stderr_task)
        try:
            # Release may have started while subprocess creation yielded.
            self._check_not_destroying(sandbox.sandbox_id)
            self.get(sandbox.sandbox_id, sandbox.generation)
            try:
                # A leader may exit while its descendants still hold output
                # pipes. The deadline covers both exit and output drainage.
                await asyncio.wait_for(asyncio.shield(completion), timeout=timeout)
            except TimeoutError:
                status = "TIMED_OUT"
                await self._terminate(process)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(completion),
                        timeout=max(0.1, self.settings.terminate_grace_seconds),
                    )
                except TimeoutError:
                    # A descendant may escape the process group at basic
                    # isolation. Never let its inherited pipes hang the API.
                    stdout_task.cancel()
                    stderr_task.cancel()
                    completion.cancel()
                    await asyncio.gather(completion, return_exceptions=True)
            (stdout_b, stdout_cut), (stderr_b, stderr_cut) = await asyncio.gather(
                stdout_task, stderr_task
            )
        except BaseException:
            await self._terminate(process)
            stdout_task.cancel()
            stderr_task.cancel()
            completion.cancel()
            await asyncio.gather(completion, stdout_task, stderr_task, return_exceptions=True)
            raise
        finally:
            self.processes.pop((sandbox.sandbox_id, request.exec_id), None)
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        if status == "SUCCEEDED" and process.returncode != 0:
            status = "FAILED"
        self.touch(sandbox.sandbox_id)
        return ExecResponse(
            exec_id=request.exec_id,
            status=status,
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - started) * 1000),
            truncated=stdout_cut or stderr_cut,
        )

    def supports_templates(self) -> bool:
        return True

    def attach_templates(
        self, sandbox_id: str, records: Sequence[TemplateRecord]
    ) -> list[dict[str, Any]]:
        """Bind templates to a sandbox and materialize them now.

        Materializing at attach time rather than at first exec means a missing
        or corrupt template surfaces as a clear error on the call that asked for
        it, instead of as a confusing failure inside an unrelated command.
        """
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None:
            raise RuntimeError("STALE_SANDBOX_GENERATION")
        with self._template_lock(sandbox_id), self._execution_lock(sandbox, None):
            self._check_not_destroying(sandbox_id)
            self.get(sandbox_id, sandbox.generation)
            resolved = self.templates.resolve(records)
            self._attached_templates[sandbox_id] = tuple(records)
            return [{**record.as_dict(), "local_path": str(path)} for record, path in resolved]

    def pinned_templates(self) -> set[tuple[str, str]]:
        """Revisions currently bound to a sandbox, which pruning must not evict."""
        return {
            (record.name, record.digest)
            for records in self._attached_templates.values()
            for record in records
        }

    async def prune_template_cache(self) -> list[tuple[str, str]]:
        """Evict cached template revisions no sandbox is using, and report them.

        Disk is what bounds how many sandboxes a worker can host, and a cache of
        environment templates is the largest thing this project writes to it, so
        the budget in `SANDBOX_TEMPLATE_CACHE_MAX_BYTES` is only real if
        something enforces it. A revision bound to a live sandbox is pinned:
        it is bind-mounted into that sandbox's namespace, and removing the
        source of a bind mount breaks a sandbox that is still running.
        """
        evicted = await asyncio.to_thread(
            self.templates.cache.prune, pinned=self.pinned_templates()
        )
        if evicted:
            logger.info(
                "template cache pruned %d revision(s) to stay within the configured budget: %s",
                len(evicted),
                ", ".join(f"{name}@{digest}" for name, digest in evicted),
            )
        return evicted

    def _resolve_templates(self, sandbox: LocalSandbox) -> tuple[SandboxMount, ...]:
        records = self._attached_templates.get(sandbox.sandbox_id, ())
        if not records:
            return ()
        return tuple(
            SandboxMount(path, record.mount_target, "ro-bind")
            for record, path in self.templates.resolve(records)
        )

    def build_template(
        self,
        sandbox: LocalSandbox,
        *,
        name: str,
        source_path: str = "/envs",
        description: str = "",
        labels: dict[str, str] | None = None,
    ) -> TemplateRecord:
        """Snapshot part of a sandbox into a reusable template.

        This is the path that makes templates practical: set an environment up
        once interactively, then promote it, instead of having to describe it in
        a Dockerfile and rebuild an image.
        """
        with self._template_lock(sandbox.sandbox_id):
            self._check_not_destroying(sandbox.sandbox_id)
            self.get(sandbox.sandbox_id, sandbox.generation)
            with self._execution_lock(sandbox, None):
                source = _resolve_sandbox_path(sandbox, source_path)
                parts = source.relative_to(sandbox.root).parts
                with contextlib.ExitStack() as stack:
                    try:
                        directory = stack.enter_context(open_directory(sandbox.root, parts))
                    except OSError as exc:
                        raise RuntimeError("SANDBOX_TEMPLATE_SOURCE_NOT_A_DIRECTORY") from exc
                    # Storage failures are not invalid-source errors. Translate
                    # only the no-follow source opening, not upload/cache I/O.
                    return self.templates.build(
                        name=name,
                        source=directory,
                        description=description,
                        source_sandbox_id=sandbox.sandbox_id,
                        labels=labels,
                        mount_target=f"{TEMPLATE_MOUNT_ROOT}/{name}",
                    )

    def _check_not_dormant(self, sandbox: LocalSandbox) -> None:
        if sandbox.sandbox_id in self.dormant:
            raise RuntimeError("SANDBOX_SUSPENDED")
        current = self.sandboxes.get(sandbox.sandbox_id)
        if current is not None and current.generation != sandbox.generation:
            raise RuntimeError("STALE_SANDBOX_GENERATION")

    def _check_not_destroying(self, sandbox_id: str) -> None:
        if sandbox_id in self._destroying:
            raise RuntimeError("SANDBOX_RELEASING")

    @contextlib.contextmanager
    def _template_lock(self, sandbox_id: str, *, wait: bool = False) -> Iterator[None]:
        # Stable across root removal, and never reachable from an agent mount.
        lock_root = self.settings.template_cache_root / ".lifecycle-locks"
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = hashlib.sha256(sandbox_id.encode()).hexdigest()
        with (lock_root / f"{key}.lock").open("a+b") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
                yield
            except BlockingIOError as exc:
                raise RuntimeError("SANDBOX_TEMPLATE_OPERATION_LOCKED") from exc
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    async def cancel(self, sandbox_id: str, exec_id: str) -> bool:
        process = self.processes.get((sandbox_id, exec_id))
        if process is None:
            return False
        await self._terminate(process)
        return True

    async def destroy(self, sandbox_id: str, *, delete_files: bool = True) -> None:
        existing = self._destroy_tasks.get(sandbox_id)
        if existing is not None:
            await complete_before_cancelling(existing)
            return
        self._destroying.add(sandbox_id)
        worker = asyncio.create_task(self._destroy(sandbox_id, delete_files=delete_files))
        self._destroy_tasks[sandbox_id] = worker
        await complete_before_cancelling(worker)

    async def _destroy(self, sandbox_id: str, *, delete_files: bool) -> None:
        try:
            # Do not wait for an execution lock before terminating commands.
            for (sid, _), process in list(self.processes.items()):
                if sid == sandbox_id:
                    await self._terminate(process)
            await complete_in_thread(self._destroy_files, sandbox_id, delete_files)
        finally:
            self._destroying.discard(sandbox_id)
            self._destroy_tasks.pop(sandbox_id, None)

    def _destroy_files(self, sandbox_id: str, delete_files: bool) -> None:
        with self._template_lock(sandbox_id, wait=True):
            root = _sandbox_root(self.settings.workspace_root, sandbox_id)
            if root.exists():
                lock_root = root / ".exec-locks"
                lock_root.mkdir(mode=0o700, exist_ok=True)
                with (lock_root / "global.lock").open("a+b") as stream:
                    # Drain in-flight File API work and terminated executions.
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    self._remove_sandbox(sandbox_id, delete_files=delete_files)
            else:
                self._remove_sandbox(sandbox_id, delete_files=delete_files)

    def _remove_sandbox(self, sandbox_id: str, *, delete_files: bool) -> None:
        sandbox = self.sandboxes.pop(sandbox_id, None)
        self.last_active_at.pop(sandbox_id, None)
        self.dormant.pop(sandbox_id, None)
        # Unpin the revisions this sandbox held. Leaving them here would keep
        # every revision ever attached to this worker in the cache for as long
        # as the process lives, because pruning never evicts a pinned revision.
        self._attached_templates.pop(sandbox_id, None)
        if delete_files:
            root = (
                sandbox.root if sandbox else _sandbox_root(self.settings.workspace_root, sandbox_id)
            )
            if root.exists():
                trash_root = self.settings.workspace_root / ".trash"
                trash_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                trash_path = trash_root / f"{sandbox_id}-{uuid.uuid4().hex}"
                try:
                    os.replace(root, trash_path)
                except OSError:
                    # Some shared filesystems do not support the atomic rename this relies
                    # on. Wait for the delete to finish there, so a rebuilt sandbox with
                    # the same id is not removed later by the background task.
                    shutil.rmtree(root, True)

    # ── suspend and resume ──
    #
    # A suspended sandbox keeps its directory and gives up its slot: it leaves
    # `sandboxes`, which is what the heartbeat counts as running sessions. No
    # process is frozen; suspend refuses while anything is running.

    async def suspend(
        self, sandbox_id: str, generation: int, *, snapshot: bool = False
    ) -> dict[str, object]:
        """Release the capacity slot of an idle sandbox, keeping its files.

        Idempotent: suspending a sandbox that is already dormant at this
        generation reports success and changes nothing.
        """
        return await complete_in_thread(self._suspend_sandbox, sandbox_id, generation, snapshot)

    def _suspend_sandbox(
        self, sandbox_id: str, generation: int, snapshot: bool
    ) -> dict[str, object]:
        self._check_not_destroying(sandbox_id)
        with self._template_lock(sandbox_id):
            self._check_not_destroying(sandbox_id)
            sandbox = self.sandboxes.get(sandbox_id)
            if sandbox is None:
                dormant = self.dormant.get(sandbox_id)
                if dormant is not None and dormant.generation == generation:
                    return {"status": "SUSPENDED", "snapshot": dormant.snapshot}
                raise RuntimeError("STALE_SANDBOX_GENERATION")
            if sandbox.generation != generation:
                raise RuntimeError("STALE_SANDBOX_GENERATION")
            if self._has_processes(sandbox_id):
                raise RuntimeError("SANDBOX_SUSPEND_BUSY")
            try:
                with self._execution_lock(sandbox, None):
                    # Exclusive and nonblocking: any command or file call still
                    # inside this sandbox makes the suspend fail cleanly, and
                    # none can start until the sandbox is unregistered below.
                    if self._has_processes(sandbox_id):
                        raise RuntimeError("SANDBOX_SUSPEND_BUSY")
                    store = self.templates.object_store
                    if snapshot:
                        if store is None:
                            raise RuntimeError("SANDBOX_SNAPSHOT_UNAVAILABLE")
                        write_snapshot(
                            store,
                            sandbox_id=sandbox_id,
                            generation=generation,
                            root=sandbox.root,
                            staging_root=self._staging_root(),
                            limits=self.templates.snapshot_limits,
                        )
                    self.sandboxes.pop(sandbox_id, None)
                    self.last_active_at.pop(sandbox_id, None)
                    self.dormant[sandbox_id] = DormantSandbox(
                        sandbox_id=sandbox_id,
                        generation=generation,
                        uid=sandbox.uid,
                        suspended_at=time.time(),
                        snapshot=snapshot,
                        # Kept, but not pinned: a dormant sandbox has nothing
                        # mounted, and a resume materializes them again.
                        templates=self._attached_templates.pop(sandbox_id, ()),
                    )
            except RuntimeError as exc:
                if str(exc) in {"SANDBOX_SHARED_WORKSPACE_LOCKED", "SANDBOX_EXEC_SCOPE_LOCKED"}:
                    raise RuntimeError("SANDBOX_SUSPEND_BUSY") from exc
                raise
        return {"status": "SUSPENDED", "snapshot": snapshot}

    async def resume(
        self, sandbox_id: str, generation: int, uid: int, *, restore: str = "reuse"
    ) -> tuple[LocalSandbox, str]:
        """Register a dormant sandbox again and return it with where its files came from.

        `restore="reuse"` keeps the local directory and falls back to the
        snapshot only when the directory is gone; `restore="snapshot"` replaces
        whatever is local with the snapshot, which is what a sandbox moving to a
        different worker needs. Idempotent for an already registered sandbox.
        """
        if restore not in {"reuse", "snapshot"}:
            raise ValueError("restore must be 'reuse' or 'snapshot'")
        return await complete_in_thread(self._resume_sandbox, sandbox_id, generation, uid, restore)

    def _resume_sandbox(
        self, sandbox_id: str, generation: int, uid: int, restore: str
    ) -> tuple[LocalSandbox, str]:
        self._check_not_destroying(sandbox_id)
        with self._template_lock(sandbox_id):
            self._check_not_destroying(sandbox_id)
            existing = self.sandboxes.get(sandbox_id)
            if existing and existing.generation == generation and existing.uid == uid:
                self.touch(sandbox_id)
                return existing, "active"
            root = _sandbox_root(self.settings.workspace_root, sandbox_id)
            source = "reused"
            if restore == "snapshot" or not root.exists():
                self._restore_locked(sandbox_id, uid, root)
                source = "restored"
            dormant = self.dormant.pop(sandbox_id, None)
            self.sandboxes.pop(sandbox_id, None)
            sandbox = self._create_sandbox_locked(sandbox_id, generation, uid)
            if dormant is not None and dormant.templates:
                self._attached_templates[sandbox_id] = dormant.templates
            return sandbox, source

    def _restore_locked(self, sandbox_id: str, uid: int, root: Path) -> None:
        store = self.templates.object_store
        if store is None:
            raise RuntimeError("SANDBOX_WORKSPACE_LOST")
        if not self.disk_status()["available"]:
            raise RuntimeError("SANDBOX_WORKER_DISK_PRESSURE")
        staging = self._staging_root() / f"restore-{uuid.uuid4().hex}"
        try:
            manifest = restore_snapshot(
                store,
                sandbox_id=sandbox_id,
                destination=staging,
                uid=uid,
                max_extract_bytes=self.settings.template_max_extract_bytes,
            )
            if manifest is None:
                raise RuntimeError("SANDBOX_WORKSPACE_LOST")
            if root.exists():
                # A stale local copy loses to the snapshot it predates.
                self._move_to_trash(sandbox_id, root)
            os.replace(staging, root)
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def _staging_root(self) -> Path:
        # Beside the sandboxes, so the final rename stays on one filesystem, and
        # under a reserved name no sandbox id can take.
        path = self.settings.workspace_root / ".staging"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def _move_to_trash(self, sandbox_id: str, root: Path) -> Path | None:
        trash_root = self.settings.workspace_root / ".trash"
        trash_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = trash_root / f"{sandbox_id}-{uuid.uuid4().hex}"
        try:
            os.replace(root, target)
        except OSError:
            shutil.rmtree(root, True)
            return None
        return target

    # ── orphaned workspace directories ──
    #
    # A sandbox that resumed on another worker from its snapshot, or that was
    # released while this worker was down, leaves its directory here. Only the
    # control plane can tell that from the metadata store, so it passes in the
    # ids it found orphaned; this side keeps the clock and does the deletion.
    # The clock is a marker file per directory, created the first time the
    # directory is reported orphaned, so a restarted worker keeps counting
    # instead of starting over, and a directory that becomes owned again (or
    # disappears) loses its marker.

    def _orphan_marker_root(self) -> Path:
        # Beside the lifecycle locks, outside every sandbox mount, and skipped
        # by the template cache because of the leading dot.
        return self.settings.template_cache_root / ".orphan-workspaces"

    def _owned_here(self, sandbox_id: str) -> bool:
        # Only what runs here, or is being released here, is protected by
        # memory. A dormant entry is not: whether a sandbox is still suspended
        # on this worker is the route's to say, and a route released while this
        # worker was unreachable leaves a dormant entry behind that nothing
        # else clears. The control plane never reports a sandbox whose route
        # still names this worker, so a sandbox suspended here is never
        # reported orphaned.
        return sandbox_id in self.sandboxes or sandbox_id in self._destroying

    async def local_workspace_ids(self) -> list[str]:
        """Directories under the workspace root that no live sandbox holds."""
        return await asyncio.to_thread(self._local_workspace_ids)

    def _local_workspace_ids(self) -> list[str]:
        root = self.settings.workspace_root
        if not root.is_dir():
            return []
        found: list[str] = []
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name in _RESERVED_ROOT_NAMES or entry.name.startswith("."):
                    continue
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if self._owned_here(entry.name):
                    continue
                found.append(entry.name)
        return sorted(found)

    async def reclaim_orphan_workspaces(
        self, orphaned: list[str], *, ttl_seconds: int
    ) -> list[str]:
        """Delete the directories that have been orphaned for at least `ttl_seconds`.

        `orphaned` is the full set the control plane found this cycle: a marker
        for any id not in it is dropped, so ownership coming back resets the
        clock. Returns the ids whose directory was deleted.
        """
        return await asyncio.to_thread(
            self._reclaim_orphan_workspaces, set(orphaned), ttl_seconds, time.time()
        )

    def _reclaim_orphan_workspaces(
        self, orphaned: set[str], ttl_seconds: int, now: float
    ) -> list[str]:
        markers = self._orphan_marker_root()
        markers.mkdir(mode=0o700, parents=True, exist_ok=True)
        for marker in list(markers.iterdir()):
            if marker.name not in orphaned:
                marker.unlink(missing_ok=True)
        removed: list[str] = []
        for sandbox_id in sorted(orphaned):
            try:
                root = _sandbox_root(self.settings.workspace_root, sandbox_id)
            except ValueError:
                continue
            marker = markers / sandbox_id
            if root.is_symlink() or not root.is_dir() or self._owned_here(sandbox_id):
                marker.unlink(missing_ok=True)
                continue
            try:
                first_seen = marker.stat().st_mtime
            except FileNotFoundError:
                marker.touch(mode=0o600)
                continue
            if now - first_seen < ttl_seconds:
                continue
            try:
                with self._template_lock(sandbox_id):
                    # Re-checked under the lock every create, resume and
                    # release takes: a sandbox that came back here meanwhile
                    # keeps its directory.
                    if self._owned_here(sandbox_id):
                        marker.unlink(missing_ok=True)
                        continue
                    # A dormant entry for a route released or moved elsewhere
                    # is stale; it goes with the directory.
                    self.dormant.pop(sandbox_id, None)
                    self._attached_templates.pop(sandbox_id, None)
                    if root.exists() and (trashed := self._move_to_trash(sandbox_id, root)):
                        shutil.rmtree(trashed, True)
                    marker.unlink(missing_ok=True)
                    removed.append(sandbox_id)
            except RuntimeError:
                # Busy: a create, resume or release holds the lock. Next cycle.
                continue
        return removed

    def _has_processes(self, sandbox_id: str) -> bool:
        return any(sid == sandbox_id for sid, _ in self.processes)

    async def reclaim_dormant_disk(self) -> list[str]:
        """Evict local copies of snapshotted dormant sandboxes under disk pressure.

        Oldest first, and only while the disk is over its watermark. A sandbox
        with no snapshot is never evicted here: its directory is the only copy,
        and only retention expiry (a release) may delete it. An evicted sandbox
        stays suspended; its resume restores from the snapshot.
        """
        evicted: list[str] = []
        if self.disk_status()["available"]:
            return evicted
        candidates = sorted(
            (item for item in self.dormant.values() if item.snapshot and not item.evicted),
            key=lambda item: item.suspended_at,
        )
        for item in candidates:
            if await asyncio.to_thread(self._evict_dormant, item.sandbox_id):
                evicted.append(item.sandbox_id)
                logger.info("evicted dormant workspace under disk pressure: %s", item.sandbox_id)
            if self.disk_status()["available"]:
                break
        return evicted

    async def adopt_dormant(
        self,
        sandbox_id: str,
        *,
        generation: int,
        uid: int,
        suspended_at: float,
        snapshot: bool,
    ) -> bool:
        """Remember a dormant directory this process has forgotten, so disk pressure can evict it.

        A restart empties `dormant`, and the directories it described stay on
        disk. The control plane knows which suspended routes still name this
        worker and whether their snapshot exists; this takes that back. It
        changes no file. Returns whether the sandbox is newly remembered.
        """
        return await asyncio.to_thread(
            self._adopt_dormant, sandbox_id, generation, uid, suspended_at, snapshot
        )

    def _adopt_dormant(
        self, sandbox_id: str, generation: int, uid: int, suspended_at: float, snapshot: bool
    ) -> bool:
        try:
            root = _sandbox_root(self.settings.workspace_root, sandbox_id)
            with self._template_lock(sandbox_id):
                if (
                    sandbox_id in self.sandboxes
                    or sandbox_id in self.dormant
                    or sandbox_id in self._destroying
                    or root.is_symlink()
                    or not root.is_dir()
                ):
                    return False
                self.dormant[sandbox_id] = DormantSandbox(
                    sandbox_id=sandbox_id,
                    generation=generation,
                    uid=uid,
                    suspended_at=suspended_at,
                    snapshot=snapshot,
                )
                return True
        except (RuntimeError, ValueError):
            # A create, resume or release holds the lock, or the id is not a
            # directory name: leave it to the next cycle.
            return False

    def _evict_dormant(self, sandbox_id: str) -> bool:
        try:
            with self._template_lock(sandbox_id):
                item = self.dormant.get(sandbox_id)
                if item is None or not item.snapshot or sandbox_id in self.sandboxes:
                    return False
                root = _sandbox_root(self.settings.workspace_root, sandbox_id)
                if root.exists() and (trashed := self._move_to_trash(sandbox_id, root)):
                    # Removed now rather than by the next sweep: the point is
                    # to free the disk this cycle.
                    shutil.rmtree(trashed, True)
                item.evicted = True
                return True
        except RuntimeError:
            # A resume or release holds the lock; it decides this sandbox.
            return False

    async def shutdown(self) -> None:
        for process in list(self.processes.values()):
            await self._terminate(process)
        self.processes.clear()

    async def write_file(self, sandbox: LocalSandbox, path: str, content_base64: str) -> None:
        content = base64.b64decode(content_base64, validate=True)
        if len(content) > self.settings.max_file_api_bytes:
            raise ValueError("file exceeds the sandbox File API size limit")
        with self._path_lock(sandbox, path):
            WorkspaceFiles(sandbox.workspace, sandbox.uid).write(path, content)
        self.touch(sandbox.sandbox_id)

    async def read_file(self, sandbox: LocalSandbox, path: str) -> str:
        with self._path_lock(sandbox, path, shared=True):
            data = WorkspaceFiles(sandbox.workspace, sandbox.uid).read(
                path, self.settings.max_file_api_bytes
            )
            content = base64.b64encode(data).decode()
        self.touch(sandbox.sandbox_id)
        return content

    async def list_directory(
        self, sandbox: LocalSandbox, path: str, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """List one directory, without following symlinks out of the workspace.

        Entries are described with `lstat`, so a symlink is reported as a
        symlink rather than silently taking on the identity of whatever it
        points at. Sorting is stable and applied before paging, or a caller
        walking pages would see entries shift between requests.
        """
        with self._path_lock(sandbox, path, shared=True):
            entries, total, has_more = WorkspaceFiles(sandbox.workspace, sandbox.uid).list(
                path, limit=limit, offset=offset
            )
        self.touch(sandbox.sandbox_id)
        return entries, total, has_more

    async def make_directory(self, sandbox: LocalSandbox, path: str, *, parents: bool) -> None:
        with self._path_lock(sandbox, path):
            WorkspaceFiles(sandbox.workspace, sandbox.uid).mkdir(path, parents=parents)
        self.touch(sandbox.sandbox_id)

    async def delete_path(self, sandbox: LocalSandbox, path: str, *, recursive: bool) -> None:
        """Delete a file, symlink, or directory inside the workspace.

        A symlink is unlinked rather than followed, so deleting a link that
        points outside the workspace removes the link and leaves the target
        alone.
        """
        with self._path_lock(sandbox, path):
            WorkspaceFiles(sandbox.workspace, sandbox.uid).delete(path, recursive=recursive)
        self.touch(sandbox.sandbox_id)

    async def move_path(
        self, sandbox: LocalSandbox, source: str, destination: str, *, overwrite: bool
    ) -> None:
        """Move or rename within the workspace.

        Both parents are pinned with no-follow directory descriptors; a final
        symlink is moved as an entry, never followed.
        """
        if workspace_parts(source) == workspace_parts(destination):
            if not workspace_parts(source):
                raise ValueError("cannot move the workspace root")
            return
        # Lock both ends in a stable order so two concurrent moves that cross
        # over each other cannot deadlock.
        first, second = sorted(
            "/workspace/" + "/".join(workspace_parts(path)) for path in (source, destination)
        )
        with self._path_lock(sandbox, first), self._path_lock(sandbox, second):
            WorkspaceFiles(sandbox.workspace, sandbox.uid).move(
                source, destination, overwrite=overwrite
            )
        self.touch(sandbox.sandbox_id)

    def touch(self, sandbox_id: str) -> None:
        self.last_active_at[sandbox_id] = time.monotonic()

    def idle_sandboxes(self) -> list[LocalSandbox]:
        cutoff = time.monotonic() - self.settings.idle_ttl_seconds
        active_ids = {sandbox_id for sandbox_id, _ in self.processes}
        return [
            sandbox
            for sandbox_id, sandbox in self.sandboxes.items()
            if sandbox_id not in active_ids and self.last_active_at.get(sandbox_id, 0) <= cutoff
        ]

    def disk_status(self) -> dict[str, int | bool]:
        root = self.settings.workspace_root
        probe_path = root if root.exists() else root.parent
        usage = shutil.disk_usage(probe_path)
        used_percent = int((usage.used * 100) / max(usage.total, 1))
        available = (
            usage.free >= self.settings.min_free_bytes
            and used_percent < self.settings.disk_high_watermark_percent
        )
        return {
            "available": available,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "used_percent": used_percent,
        }

    async def cleanup_trash(self) -> None:
        trash_root = self.settings.workspace_root / ".trash"
        if not trash_root.exists():
            return
        for path in list(trash_root.iterdir()):
            if path.is_dir() and not path.is_symlink():
                await asyncio.to_thread(shutil.rmtree, path, True)
            else:
                await asyncio.to_thread(path.unlink, missing_ok=True)

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            if process.returncode is None:
                raise
            # Some POSIX hosts report EPERM for an orphaned zombie group.
            logger.warning("cannot signal process group of exited command %s", process.pid)
            return
        try:
            await asyncio.wait_for(
                process.wait(), timeout=max(0.1, self.settings.terminate_grace_seconds)
            )
        except TimeoutError:
            pass
        finally:
            # Always target remaining group members, even if the leader has
            # exited or a child ignored TERM. returncode describes only the leader.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.returncode is None:
                    raise
                logger.warning("cannot kill process group of exited command %s", process.pid)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                process.wait(), timeout=max(0.1, self.settings.terminate_grace_seconds)
            )


def _initialize_top_level(root: Path, uid: int, names: tuple[str, ...]) -> None:
    for path in (root, *(root / name for name in names)):
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"illegal sandbox top-level directory: {path}")
        os.chown(path, uid, uid, follow_symlinks=False)
        os.chmod(path, 0o700)


def _validate_top_level(root: Path, uid: int, names: tuple[str, ...]) -> None:
    for path in (root, *(root / name for name in names)):
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"sandbox top-level directory is illegal or missing: {path}")
        if path.stat(follow_symlinks=False).st_uid != uid:
            raise RuntimeError(f"sandbox top-level directory has a mismatched UID: {path}")
        os.chmod(path, 0o700)


def _runtime_profile(
    settings: Settings,
    *,
    disable_nested_userns: bool,
    isolation_level: IsolationLevel = IsolationLevel.BASIC,
    additional_features: tuple[str, ...] = (),
) -> SandboxRuntimeProfile:
    return SandboxRuntimeProfile(
        profile_id=settings.profile_id,
        profile_hash=settings.profile_hash,
        bubblewrap_path=settings.bubblewrap_path,
        setpriv_path=settings.setpriv_path,
        prlimit_path=settings.prlimit_path,
        minimum_version=settings.bubblewrap_min_version,
        disable_nested_userns=disable_nested_userns,
        isolation_level=isolation_level,
        additional_features=additional_features,
        network_isolated=settings.network_mode == "isolated",
        tmpfs_bytes=settings.tmpfs_bytes,
        cpu_seconds=settings.cpu_seconds,
        max_processes=settings.max_processes,
        max_open_files=settings.max_open_files,
        max_file_size_bytes=settings.max_file_size_bytes,
        readonly_mounts=tuple(settings.readonly_mounts),
    )


def _workspace_path(workspace: Path, virtual_path: str) -> Path:
    parts = PurePosixPath(virtual_path).parts
    if ".." in parts:
        raise ValueError("file path may not contain ..")
    # Strip the mount prefix with and without its trailing slash. Matching only
    # `/workspace/` would leave a bare `/workspace` to be treated as the
    # relative path `workspace`, resolving to `<workspace>/workspace`.
    normalized = virtual_path.rstrip("/") or "/workspace"
    if normalized == "/workspace":
        relative = ""
    else:
        relative = normalized.removeprefix("/workspace/").lstrip("/")
    target = (workspace / relative).resolve(strict=False)
    root = workspace.resolve(strict=True)
    if root not in {target, *target.parents}:
        raise ValueError("file path escapes the workspace")
    return target


# In-sandbox paths that may be snapshotted into a template. `/envs` is the
# point of the feature; `/workspace` lets a project vendor its own tree. `/home`
# and `/cache` are excluded because they hold credentials and throwaway state.
_TEMPLATE_SOURCE_ROOTS = {"/envs": "envs", "/workspace": "workspace"}


def _resolve_sandbox_path(sandbox: LocalSandbox, virtual_path: str) -> Path:
    """Lexical mapping only; callers must open every component without following links."""
    normalized = "/" + virtual_path.strip().strip("/")
    parts = PurePosixPath(normalized).parts
    if ".." in parts or "\x00" in normalized:
        raise ValueError("template source path may not contain .. or NUL")
    for mount, directory in _TEMPLATE_SOURCE_ROOTS.items():
        if normalized == mount or normalized.startswith(f"{mount}/"):
            base = sandbox.root / directory
            relative = normalized.removeprefix(mount).lstrip("/")
            return base / relative
    allowed = ", ".join(sorted(_TEMPLATE_SOURCE_ROOTS))
    raise ValueError(f"template source path must live under {allowed}")


_RESERVED_ROOT_NAMES = frozenset({".trash", ".staging"})


def _sandbox_root(workspace_root: Path, sandbox_id: str) -> Path:
    if sandbox_id in _RESERVED_ROOT_NAMES:
        raise ValueError("sandbox id uses a reserved name")
    root = workspace_root.resolve(strict=False)
    candidate = (root / sandbox_id).resolve(strict=False)
    if root not in candidate.parents:
        raise ValueError("sandbox id escapes the workspace root")
    return candidate


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    kept = 0
    truncated = False
    try:
        while chunk := await stream.read(64 * 1024):
            kept_from_chunk = 0
            if kept < limit:
                part = chunk[: limit - kept]
                chunks.append(part)
                kept += len(part)
                kept_from_chunk = len(part)
            if len(chunk) > kept_from_chunk:
                truncated = True
    except asyncio.CancelledError:
        # Return what was collected when a timed-out pipe cannot be drained.
        truncated = True
    return b"".join(chunks), truncated
