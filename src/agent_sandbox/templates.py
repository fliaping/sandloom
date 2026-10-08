"""Reusable environment templates.

A sandbox starts with an empty `/envs`, so every sandbox that needs NumPy, or a
node_modules tree, or a compiler toolchain pays the install cost again. That is
the dominant startup cost for real workloads and it is pure waste: the same
dependency set gets rebuilt thousands of times.

A template is that tree, built once and then mounted read-only into every
sandbox that asks for it.

Two design choices carry the feature:

*Templates are content-addressed.* The identity of a template is the digest of
its archive, so a cached copy is provably the right one, two workers can never
disagree about what a name means, and re-materializing is idempotent.

*Templates are mounted read-only at the path they were built at.* A virtualenv
records absolute paths in its shebangs and `pyvenv.cfg`, so it only works if
`/envs/myenv` means the same thing at build time and at use time. Mounting
read-only also means every sandbox on a worker shares one page cache, which is
what makes high density cheap. A sandbox that needs to modify a template builds
a new one instead.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from .archive import ArchiveError, ArchiveLimits, snapshot_directory
from .blobstore import create_object_store

if TYPE_CHECKING:
    from .config import Settings

TEMPLATE_MOUNT_ROOT = "/envs"
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
# Templates are built by `tar`-ing a directory, so a member that escapes the
# destination or points outside it is either a bug or an attack.
_UNSAFE_TYPES = frozenset({tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE})


class TemplateError(RuntimeError):
    """A template is malformed, missing, or failed to materialize."""


@dataclass(frozen=True, slots=True)
class TemplateRef:
    """A name, optionally pinned to an exact digest.

    An unpinned ref resolves through the catalog to whatever digest the name
    currently points at. A pinned ref is reproducible and is what the control
    plane stores once a sandbox has been created.
    """

    name: str
    digest: str | None = None

    @classmethod
    def parse(cls, value: str) -> TemplateRef:
        """Parse `name` or `name@sha256:<hex>`."""
        text = value.strip()
        name, separator, digest = text.partition("@")
        validate_template_name(name)
        if not separator:
            return cls(name=name)
        validate_template_digest(digest)
        return cls(name=name, digest=digest)

    @property
    def mount_target(self) -> str:
        return f"{TEMPLATE_MOUNT_ROOT}/{self.name}"

    def __str__(self) -> str:
        return self.name if self.digest is None else f"{self.name}@{self.digest}"


@dataclass(frozen=True, slots=True)
class TemplateRecord:
    """Catalog entry describing one immutable template revision."""

    name: str
    digest: str
    size_bytes: int
    mount_target: str
    created_at: float
    description: str = ""
    source_sandbox_id: str | None = None
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def ref(self) -> TemplateRef:
        return TemplateRef(name=self.name, digest=self.digest)

    @property
    def object_key(self) -> str:
        return object_key_for(self.name, self.digest)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "mount_target": self.mount_target,
            "created_at": self.created_at,
            "description": self.description,
            "source_sandbox_id": self.source_sandbox_id,
            "labels": dict(self.labels),
        }


def validate_template_name(name: str) -> str:
    """Names become both a path segment and an object key, so keep them strict."""
    # Checked before the pattern, because the pattern's first character has to
    # be alphanumeric and therefore already excludes these: the guard is here for
    # the day someone relaxes it to allow a leading dot, at which point ".."
    # would become a path segment. Ordering it first keeps it reachable -- and
    # therefore testable -- rather than a branch nothing can take.
    if name in {".", ".."}:
        raise TemplateError(f"invalid template name {name!r}")
    if not _NAME_PATTERN.match(name):
        raise TemplateError(
            f"invalid template name {name!r}: expected lowercase alphanumerics, "
            "'.', '_' or '-', up to 64 characters"
        )
    return name


def validate_template_digest(digest: str) -> str:
    if not _DIGEST_PATTERN.match(digest):
        raise TemplateError(f"invalid template digest {digest!r}: expected 'sha256:<64 hex>'")
    return digest


def object_key_for(name: str, digest: str) -> str:
    """Object-store key. The digest is in the path, so keys are immutable."""
    return f"templates/{name}/{digest.replace(':', '-')}.tar.gz"


class TemplateObjectStore(Protocol):
    """The subset of the object store that template distribution needs.

    `download_to` raises `FileNotFoundError` when the object is absent. That is
    part of the interface rather than an accident of one backend: the catalog is
    legitimately missing before the first publish, and the only way to tell that
    apart from a store that cannot download at all is for absence to have its
    own error. Collapsing the two once left every worker but the builder
    believing that no templates existed.
    """

    def upload_file(self, key: str, path: str | Path) -> str: ...
    def download_to(self, key_or_uri: str, destination: str | Path) -> None: ...
    def delete(self, key_or_uri: str) -> None: ...


def template_object_store(settings: Settings) -> TemplateObjectStore | None:
    """The channel templates are shared over, or None for a single-worker install.

    Both halves of the feature have to use the same one: the catalog that says a
    template exists, and the transfer that moves its bytes. Giving the store to
    one and not the other produces the worst of both — a name every worker can
    resolve and no worker can fetch.
    """

    return as_template_object_store(create_object_store(settings))


def as_template_object_store(store: object) -> TemplateObjectStore | None:
    """Narrow a general object store to the file-level API templates need.

    The checkpoint `ObjectStore` protocol only promises `put`/`get`/`delete` on
    bytes, which would mean holding a multi-gigabyte environment in memory. A
    plugin store that lacks the streaming methods is not an error: templates
    simply stay worker-local, which is still useful for a single-node install.
    """
    if store is None:
        return None
    required = ("upload_file", "download_to", "delete")
    if not all(callable(getattr(store, name, None)) for name in required):
        return None
    return cast("TemplateObjectStore", store)


def archive_directory(
    source: Path | int, destination: Path, *, limits: ArchiveLimits | None = None
) -> tuple[str, int]:
    """Pack `source` into a reproducible gzip tarball and return (digest, size).

    Reproducibility is what makes the digest a usable cache key: the same tree
    must always produce the same bytes. Entries are therefore sorted and every
    field that records *when* or *by whom* the archive was built — mtime, uid,
    gid, owner names, and the gzip header timestamp — is zeroed. File mode and
    entry type are preserved, because an environment is useless if it loses its
    executable bits or its symlinks.
    """
    try:
        return snapshot_directory(source, destination, limits or ArchiveLimits())
    except ArchiveError as exc:
        raise TemplateError(str(exc)) from exc
    except NotADirectoryError as exc:
        raise TemplateError("template source is not a directory or traverses a symlink") from exc
    except (OSError, ValueError) as exc:
        raise TemplateError(
            "template snapshot could not be completed; source may have changed"
        ) from exc


def _safe_members(tar: tarfile.TarFile, destination: Path) -> Iterator[tarfile.TarInfo]:
    """Yield members that provably stay inside `destination`.

    Archives are the classic path-traversal vector, and a template archive can
    come from an object store shared by many tenants. Absolute member paths,
    `..` components, and device nodes are rejected rather than sanitized.

    Symlinks need a subtler rule. Every virtualenv contains
    `bin/python3.13 -> /usr/local/bin/python3.13`, so rejecting absolute
    symlinks would reject essentially every real environment. A symlink is also
    not a read: it is a path the *sandbox* resolves later, inside its own mount
    namespace, where `/usr` is the read-only base image. An absolute symlink can
    therefore only ever reach what the sandbox could already reach, and is
    allowed. A *relative* symlink escaping the tree is rejected, because no real
    environment contains one. Hard links are different again: extraction
    resolves them immediately against the host filesystem, so they must stay
    inside the tree.
    """
    root = destination.resolve()
    for member in tar.getmembers():
        if member.type in _UNSAFE_TYPES:
            raise TemplateError(f"template archive contains a device or FIFO entry: {member.name}")
        name = member.name
        if name.startswith("/") or Path(name).is_absolute():
            raise TemplateError(f"template archive contains an absolute path: {name}")
        target = (root / name).resolve()
        if target != root and root not in target.parents:
            raise TemplateError(f"template archive escapes its destination: {name}")
        if member.issym() and not Path(member.linkname).is_absolute():
            # Relative symlinks resolve against the directory holding them and
            # must stay inside the tree.
            resolved_link = (target.parent / member.linkname).resolve()
            if resolved_link != root and root not in resolved_link.parents:
                raise TemplateError(
                    "template archive contains a link escaping its destination: "
                    f"{name} -> {member.linkname}"
                )
        elif member.islnk():
            # Hard links are materialized against the host filesystem during
            # extraction, so an escaping target would expose a real file.
            link = member.linkname
            if Path(link).is_absolute():
                raise TemplateError(
                    f"template archive contains an absolute hard link: {name} -> {link}"
                )
            resolved_link = (root / link).resolve()
            if resolved_link != root and root not in resolved_link.parents:
                raise TemplateError(
                    f"template archive contains a link escaping its destination: {name} -> {link}"
                )
        yield member


def extract_archive(archive: Path, destination: Path, *, max_bytes: int | None = None) -> int:
    """Extract a template archive into `destination` and return the byte count.

    `max_bytes` bounds the *uncompressed* size, because a small archive can
    expand into an arbitrarily large tree and the cache lives on a shared disk.
    """
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(archive, mode="r:gz") as tar:
        members = []
        for member in _safe_members(tar, destination):
            total += member.size
            if max_bytes is not None and total > max_bytes:
                raise TemplateError(
                    f"template expands to more than {max_bytes} bytes; refusing to extract"
                )
            members.append(member)
        # `_safe_members` already rejected traversal, escaping links, and device
        # nodes. `filter="tar"` additionally strips setuid bits and is the
        # default from Python 3.14, so asking for it keeps behavior identical
        # across versions instead of changing under us on upgrade.
        tar.extractall(destination, members=members, filter="tar")
    return total


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize materialization across processes sharing the template root."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


class LocalTemplateCache:
    """Digest-addressed template trees on one worker's disk.

    Layout keeps every revision of a name side by side, so a rollback is a
    remount rather than a rebuild:

        <root>/<name>/<digest>/     materialized tree, read-only
        <root>/.staging/            partial extractions, never mounted
        <root>/.locks/              per-revision materialization locks
    """

    def __init__(
        self,
        root: Path,
        *,
        max_total_bytes: int | None = None,
        max_extract_bytes: int | None = None,
    ) -> None:
        self.root = Path(root)
        # Two distinct limits: how large the cache may grow before eviction, and
        # how large any single template may expand to. Conflating them would make
        # a small cache budget silently reject every template.
        self.max_total_bytes = max_total_bytes
        self.max_extract_bytes = max_extract_bytes

    @property
    def staging_root(self) -> Path:
        return self.root / ".staging"

    @property
    def lock_root(self) -> Path:
        return self.root / ".locks"

    def path_for(self, name: str, digest: str) -> Path:
        validate_template_name(name)
        validate_template_digest(digest)
        return self.root / name / digest.replace(":", "-")

    def has(self, name: str, digest: str) -> bool:
        return self.path_for(name, digest).is_dir()

    def materialize(self, name: str, digest: str, fetch: Any) -> Path:
        """Return the local tree for a revision, extracting it if necessary.

        `fetch(destination)` must place the archive at `destination`. It is only
        called when the revision is absent, and the extraction is staged then
        renamed, so a crash can never leave a half-extracted tree mounted into a
        sandbox.
        """
        target = self.path_for(name, digest)
        if target.is_dir():
            self._touch(target)
            return target

        lock_path = self.lock_root / f"{name}-{digest.replace(':', '-')}.lock"
        with _exclusive_lock(lock_path):
            # Another process may have finished while this one waited.
            if target.is_dir():
                self._touch(target)
                return target
            self.staging_root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f"{name}-", dir=self.staging_root))
            archive = staging.with_suffix(".tar.gz")
            try:
                fetch(archive)
                if not archive.exists():
                    raise TemplateError(f"fetching template {name}@{digest} produced no archive")
                actual = digest_of_file(archive)
                if actual != digest:
                    raise TemplateError(
                        f"template {name} digest mismatch: expected {digest}, got {actual}"
                    )
                extract_archive(archive, staging, max_bytes=self.max_extract_bytes)
                # A template is read-only data shared by every sandbox on this
                # worker, each running as a different unprivileged UID. mkdtemp
                # creates the staging directory 0700 and os.replace preserves
                # that, so without this the mount succeeds and every exec then
                # fails with EACCES. Only the traversal bit is added; file modes
                # from the archive are left alone.
                _grant_world_traversal(staging)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, target)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                archive.unlink(missing_ok=True)
        self._touch(target)
        return target

    def _touch(self, path: Path) -> None:
        """Record use so pruning can evict the least recently used revision."""
        with contextlib.suppress(OSError):
            os.utime(path, None)

    def list_revisions(self) -> list[tuple[str, str, int, float]]:
        """Return (name, digest, size_bytes, last_used) for every cached tree."""
        revisions: list[tuple[str, str, int, float]] = []
        if not self.root.is_dir():
            return revisions
        for name_dir in sorted(self.root.iterdir()):
            if not name_dir.is_dir() or name_dir.name.startswith("."):
                continue
            for revision in sorted(name_dir.iterdir()):
                if not revision.is_dir():
                    continue
                digest = revision.name.replace("sha256-", "sha256:", 1)
                revisions.append(
                    (name_dir.name, digest, _tree_size(revision), revision.stat().st_mtime)
                )
        return revisions

    def remove(self, name: str, digest: str) -> bool:
        target = self.path_for(name, digest)
        if not target.is_dir():
            return False
        shutil.rmtree(target, ignore_errors=True)
        with contextlib.suppress(OSError):
            target.parent.rmdir()
        return True

    def prune(self, *, pinned: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
        """Evict least-recently-used revisions until the cache fits its budget.

        `pinned` revisions are never evicted: they are bind-mounted into live
        sandboxes, and removing the source of a bind mount breaks them.
        """
        if self.max_total_bytes is None:
            return []
        protected = {(name, digest) for name, digest in pinned}
        revisions = sorted(self.list_revisions(), key=lambda item: item[3])
        total = sum(item[2] for item in revisions)
        evicted: list[tuple[str, str]] = []
        for name, digest, size, _used in revisions:
            if total <= self.max_total_bytes:
                break
            if (name, digest) in protected:
                continue
            if self.remove(name, digest):
                total -= size
                evicted.append((name, digest))
        return evicted


def _grant_world_traversal(root: Path) -> None:
    """Make every directory in a materialized template enterable by any UID.

    Sandboxes run as unprivileged users that do not own the cache, so they need
    `x` on each directory to reach the files inside. Read bits on regular files
    come from the archive and are deliberately not touched: a template that
    ships a private key still ships it with the mode its author chose.
    """
    for current, directories, _files in os.walk(root):
        for name in (current, *(os.path.join(current, item) for item in directories)):
            with contextlib.suppress(OSError):
                mode = os.stat(name).st_mode & 0o777
                os.chmod(name, mode | 0o111 | 0o044)


def digest_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _tree_size(root: Path) -> int:
    total = 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(current, name)).st_size
    return total


class TemplateCatalog:
    """Maps template names to their current revision.

    Content addressing makes a *digest* unambiguous, but users want to say
    `python-ml`, not a hash. The catalog is that indirection, and it is
    deliberately small: a JSON document per name, written atomically.

    The object store is the shared copy when one is configured, so a template
    published on one worker is resolvable on every other. The local file is
    both the cache and the fallback for single-worker deployments.
    """

    CATALOG_KEY = "templates/catalog.json"

    def __init__(self, path: Path, *, object_store: TemplateObjectStore | None = None) -> None:
        self.path = Path(path)
        self.object_store = object_store

    def _read_local(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return dict(json.loads(self.path.read_text() or "{}"))
        except (OSError, ValueError) as error:
            raise TemplateError(
                f"template catalog at {self.path} is unreadable: {error}"
            ) from error

    def _write_local(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write-and-rename: a reader must never observe a half-written catalog.
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        try:
            temporary.write_text(json.dumps(data, indent=2, sort_keys=True))
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _pull(self) -> dict[str, Any]:
        """Refresh the local copy from the shared one, if there is a shared one."""
        if self.object_store is None:
            return self._read_local()
        with tempfile.TemporaryDirectory() as scratch:
            destination = Path(scratch) / "catalog.json"
            try:
                self.object_store.download_to(self.CATALOG_KEY, destination)
            except FileNotFoundError:
                # The normal state before the first publish. Anything else is a
                # broken store, and reporting it as "no templates yet" is how a
                # download that never worked stayed invisible.
                return self._read_local()
            data = dict(json.loads(destination.read_text() or "{}"))
        self._write_local(data)
        return data

    def list(self) -> list[TemplateRecord]:
        return [_record_from_dict(item) for item in self._pull().values()]

    def get(self, name: str) -> TemplateRecord | None:
        validate_template_name(name)
        entry = self._pull().get(name)
        return None if entry is None else _record_from_dict(entry)

    def resolve(self, ref: TemplateRef) -> TemplateRecord:
        """Turn a ref into an exact revision, failing loudly when it cannot."""
        record = self.get(ref.name)
        if record is None:
            raise TemplateError(f"template {ref.name!r} is not in the catalog")
        if ref.digest is not None and ref.digest != record.digest:
            # The pinned revision may predate the current one; it is still valid
            # as long as the caller knows exactly what it wants.
            return TemplateRecord(
                name=record.name,
                digest=ref.digest,
                size_bytes=record.size_bytes,
                mount_target=record.mount_target,
                created_at=record.created_at,
                description=record.description,
                source_sandbox_id=record.source_sandbox_id,
                labels=dict(record.labels),
            )
        return record

    def publish(self, record: TemplateRecord) -> TemplateRecord:
        """Point a name at a revision, making it resolvable everywhere."""
        with _exclusive_lock(self.path.with_suffix(".lock")):
            data = self._pull()
            data[record.name] = record.as_dict()
            self._write_local(data)
            self._push(data)
        return record

    def remove(self, name: str) -> bool:
        validate_template_name(name)
        with _exclusive_lock(self.path.with_suffix(".lock")):
            data = self._pull()
            if name not in data:
                return False
            del data[name]
            self._write_local(data)
            self._push(data)
        return True

    def _push(self, data: dict[str, Any]) -> None:
        if self.object_store is None:
            return
        with tempfile.TemporaryDirectory() as scratch:
            document = Path(scratch) / "catalog.json"
            document.write_text(json.dumps(data, indent=2, sort_keys=True))
            self.object_store.upload_file(self.CATALOG_KEY, document)


def _record_from_dict(data: dict[str, Any]) -> TemplateRecord:
    return TemplateRecord(
        name=data["name"],
        digest=data["digest"],
        size_bytes=int(data.get("size_bytes", 0)),
        mount_target=data.get("mount_target") or TemplateRef(name=data["name"]).mount_target,
        created_at=float(data.get("created_at", 0.0)),
        description=data.get("description", ""),
        source_sandbox_id=data.get("source_sandbox_id"),
        labels=dict(data.get("labels") or {}),
    )


class TemplateManager:
    """Builds, distributes, and materializes environment templates.

    The object store is the distribution channel: a template built on one worker
    becomes available to every other worker without a shared filesystem. Without
    one, templates still work, but only on the worker that built them.
    """

    def __init__(
        self,
        cache: LocalTemplateCache,
        *,
        object_store: TemplateObjectStore | None = None,
        max_archive_bytes: int | None = None,
        snapshot_limits: ArchiveLimits | None = None,
    ) -> None:
        self.cache = cache
        self.object_store = object_store
        self.max_archive_bytes = max_archive_bytes
        self.snapshot_limits = snapshot_limits or ArchiveLimits(
            max_source_bytes=cache.max_extract_bytes or ArchiveLimits().max_source_bytes,
            max_archive_bytes=max_archive_bytes,
        )

    def build(
        self,
        *,
        name: str,
        source: Path | int,
        description: str = "",
        source_sandbox_id: str | None = None,
        labels: dict[str, str] | None = None,
        mount_target: str | None = None,
    ) -> TemplateRecord:
        """Snapshot a directory into an immutable, content-addressed template.

        The result is also seeded into the local cache, so the worker that built
        a template never re-downloads it.
        """
        validate_template_name(name)
        staging = self.cache.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        handle, raw_archive = tempfile.mkstemp(
            prefix=f"build-{name}-", suffix=".tar.gz", dir=staging
        )
        os.close(handle)
        archive = Path(raw_archive)
        try:
            digest, size = archive_directory(source, archive, limits=self.snapshot_limits)
            if self.max_archive_bytes is not None and size > self.max_archive_bytes:
                raise TemplateError(
                    f"template {name} archive is {size} bytes, "
                    f"over the {self.max_archive_bytes} byte limit"
                )
            record = TemplateRecord(
                name=name,
                digest=digest,
                size_bytes=size,
                mount_target=mount_target or TemplateRef(name=name).mount_target,
                created_at=time.time(),
                description=description,
                source_sandbox_id=source_sandbox_id,
                labels=dict(labels or {}),
            )
            # Seed the cache from the archive already on disk.
            if not self.cache.has(name, digest):
                self.cache.materialize(
                    name, digest, lambda destination: shutil.copyfile(archive, destination)
                )
            # Validate/materialize before distribution: invalid archives must
            # never enter a shared store, even if publication later fails.
            if self.object_store is not None:
                self.object_store.upload_file(record.object_key, archive)
            return record
        finally:
            archive.unlink(missing_ok=True)

    def materialize(self, record: TemplateRecord) -> Path:
        """Ensure a template revision is present locally and return its path."""

        def fetch(destination: Path) -> None:
            if self.object_store is None:
                raise TemplateError(
                    f"template {record.name}@{record.digest} is not cached on this worker "
                    "and no object store is configured to fetch it from"
                )
            self.object_store.download_to(record.object_key, destination)

        return self.cache.materialize(record.name, record.digest, fetch)

    def resolve(self, records: Iterable[TemplateRecord]) -> list[tuple[TemplateRecord, Path]]:
        """Materialize several templates, rejecting conflicting mount targets.

        Two different revisions claiming one path is fatal — the second would
        silently shadow the first. The same revision listed twice is not.
        """
        resolved: list[tuple[TemplateRecord, Path]] = []
        seen: dict[str, str] = {}
        for record in records:
            ref = str(record.ref)
            previous = seen.get(record.mount_target)
            if previous is not None:
                if previous == ref:
                    # The same revision listed twice is not a conflict. The list
                    # is a set of environments, and callers routinely build one by
                    # unioning several capability sets, which repeats a template.
                    # Mounting it once is exactly what was asked for.
                    continue
                # Name the revisions, not just the names: two revisions of one
                # name share a mount target, and saying "demo and demo" tells the
                # caller nothing about which two they pinned.
                raise TemplateError(
                    f"templates {previous!r} and {ref!r} both mount at {record.mount_target}"
                )
            seen[record.mount_target] = ref
            resolved.append((record, self.materialize(record)))
        return resolved


__all__ = [
    "TEMPLATE_MOUNT_ROOT",
    "LocalTemplateCache",
    "TemplateCatalog",
    "TemplateError",
    "TemplateManager",
    "TemplateObjectStore",
    "TemplateRecord",
    "TemplateRef",
    "archive_directory",
    "as_template_object_store",
    "digest_of_file",
    "extract_archive",
    "object_key_for",
    "validate_template_digest",
    "validate_template_name",
]
