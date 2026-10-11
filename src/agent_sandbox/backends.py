"""Execution backend SPI for local, remote, VM, or isolate runtimes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

from .config import Settings
from .plugins import EXECUTION_BACKEND_GROUP, create_from_plugin
from .schemas import ExecRequest, ExecResponse

if TYPE_CHECKING:
    from .runtime import LocalSandbox


class ExecutionBackend(Protocol):
    sandboxes: dict[str, Any]
    processes: dict[tuple[str, str], Any]

    async def probe(self) -> dict[str, object]: ...
    async def shutdown(self) -> None: ...
    async def cleanup_trash(self) -> None: ...
    async def create(self, sandbox_id: str, generation: int, uid: int) -> LocalSandbox: ...
    def get(self, sandbox_id: str, generation: int) -> LocalSandbox: ...
    async def execute(self, sandbox: LocalSandbox, request: ExecRequest) -> ExecResponse: ...
    async def cancel(self, sandbox_id: str, exec_id: str) -> bool: ...
    async def destroy(self, sandbox_id: str, *, delete_files: bool = True) -> None: ...
    async def write_file(self, sandbox: LocalSandbox, path: str, content_base64: str) -> None: ...
    async def read_file(self, sandbox: LocalSandbox, path: str) -> str: ...
    def disk_status(self) -> dict[str, int | bool]: ...

    # Environment templates. A backend that cannot share prebuilt environments
    # is still usable, so these are optional; `supports_templates()` reports it.
    def supports_templates(self) -> bool: ...


class TemplateCachePruning(Protocol):
    """Reclaiming cached template revisions this worker no longer uses.

    Disk is what bounds how many sandboxes one worker can host, and a cache of
    environment templates is the largest thing this project puts on it. A
    backend that materializes templates has something to reclaim; one that does
    not, does not — so this is a separate capability rather than another method
    on `ExecutionBackend`. Narrow with `as_template_cache_pruning()`.
    """

    async def prune_template_cache(self) -> list[tuple[str, str]]: ...


def as_template_cache_pruning(backend: object) -> TemplateCachePruning | None:
    """Narrow a backend to template cache pruning, or report that it cannot."""
    if backend is None:
        return None
    if not callable(getattr(backend, "prune_template_cache", None)):
        return None
    return cast("TemplateCachePruning", backend)


class DirectoryOperations(Protocol):
    """Listing and structural edits, beyond reading and writing one file.

    Separate from `ExecutionBackend` on purpose. A backend written against an
    earlier release implements only the single-file calls, and widening the
    base protocol would make every such plugin fail to satisfy it on upgrade.
    Narrow with `as_directory_operations()` instead, which reports the gap
    rather than raising deep inside a request.
    """

    async def list_directory(
        self, sandbox: LocalSandbox, path: str, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int, bool]: ...
    async def make_directory(self, sandbox: LocalSandbox, path: str, *, parents: bool) -> None: ...
    async def delete_path(self, sandbox: LocalSandbox, path: str, *, recursive: bool) -> None: ...
    async def move_path(
        self, sandbox: LocalSandbox, source: str, destination: str, *, overwrite: bool
    ) -> None: ...


def as_directory_operations(backend: object) -> DirectoryOperations | None:
    """Narrow a backend to the directory API, or report that it cannot."""
    if backend is None:
        return None
    required = ("list_directory", "make_directory", "delete_path", "move_path")
    if not all(callable(getattr(backend, name, None)) for name in required):
        return None
    return cast("DirectoryOperations", backend)


class DormantLifecycle(Protocol):
    """Suspending a sandbox to release its slot, and resuming it later.

    Optional for the same reason `DirectoryOperations` is: a backend from an
    earlier release keeps loading, and the API answers 501 for it instead.
    Narrow with `as_dormant_lifecycle()`.
    """

    dormant: dict[str, Any]

    async def suspend(
        self, sandbox_id: str, generation: int, *, snapshot: bool = False
    ) -> dict[str, object]: ...
    async def resume(
        self, sandbox_id: str, generation: int, uid: int, *, restore: str = "reuse"
    ) -> tuple[LocalSandbox, str]: ...
    async def reclaim_dormant_disk(self) -> list[str]: ...


def as_dormant_lifecycle(backend: object) -> DormantLifecycle | None:
    """Narrow a backend to suspend/resume, or report that it cannot."""
    if backend is None:
        return None
    required = ("suspend", "resume", "reclaim_dormant_disk")
    if not all(callable(getattr(backend, name, None)) for name in required):
        return None
    return cast("DormantLifecycle", backend)


class OrphanWorkspaceReclaim(Protocol):
    """Deleting workspace directories this worker no longer owns.

    The worker lists its local directories; the control plane decides which of
    them are orphaned from the metadata store; the worker deletes those that
    have stayed orphaned for the configured period. Optional, like the other
    capabilities here. Narrow with `as_orphan_workspace_reclaim()`.
    """

    async def local_workspace_ids(self) -> list[str]: ...
    async def reclaim_orphan_workspaces(
        self, orphaned: list[str], *, ttl_seconds: int
    ) -> list[str]: ...


def as_orphan_workspace_reclaim(backend: object) -> OrphanWorkspaceReclaim | None:
    """Narrow a backend to orphaned-directory reclamation, or report that it cannot."""
    if backend is None:
        return None
    required = ("local_workspace_ids", "reclaim_orphan_workspaces")
    if not all(callable(getattr(backend, name, None)) for name in required):
        return None
    return cast("OrphanWorkspaceReclaim", backend)


class DormantAdoption(Protocol):
    """Taking back a dormant sandbox this process no longer remembers.

    Which dormant directories may be evicted under disk pressure is kept in the
    worker's memory, so a restart forgets them and they would never be evicted.
    The control plane still knows which suspended routes name this worker; it
    hands those back through `adopt_dormant`. Optional. Narrow with
    `as_dormant_adoption()`.
    """

    async def adopt_dormant(
        self,
        sandbox_id: str,
        *,
        generation: int,
        uid: int,
        suspended_at: float,
        snapshot: bool,
    ) -> bool: ...


def as_dormant_adoption(backend: object) -> DormantAdoption | None:
    """Narrow a backend to dormant adoption, or report that it cannot."""
    if backend is None or not callable(getattr(backend, "adopt_dormant", None)):
        return None
    return cast("DormantAdoption", backend)


def create_execution_backend(settings: Settings) -> ExecutionBackend:
    """Load the explicitly configured backend.

    Third-party packages register factories in the
    ``agent_sandbox.execution_backends`` entry-point group.
    """
    if settings.execution_backend == "bubblewrap":
        from .runtime import SandboxRuntime

        return SandboxRuntime(settings)
    return cast(
        "ExecutionBackend",
        create_from_plugin(EXECUTION_BACKEND_GROUP, settings.execution_backend, settings),
    )


__all__ = [
    "DirectoryOperations",
    "DormantLifecycle",
    "ExecutionBackend",
    "TemplateCachePruning",
    "as_directory_operations",
    "as_dormant_lifecycle",
    "as_template_cache_pruning",
    "create_execution_backend",
]
