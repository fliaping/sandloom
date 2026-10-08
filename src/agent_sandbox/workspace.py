"""Descriptor-relative File API operations on a live, untrusted POSIX tree.

Never resolve a pathname and then use it with manager privileges: an agent
can swap any component between those steps. Pin each directory with openat
and O_NOFOLLOW instead. Parent directories of the workspace are trusted,
deployment-managed paths; agents cannot access them through their mounts.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@contextlib.contextmanager
def open_directory(
    root: Path,
    parts: tuple[str, ...] = (),
    *,
    create: bool = False,
    prepare: Callable[[int], None] | None = None,
) -> Iterator[int]:
    """Pin an untrusted directory; the ancestors of root must be trusted."""
    if any(name in {"", ".", ".."} or "/" in name or "\x00" in name for name in parts):
        raise ValueError("invalid directory component")
    fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        if prepare is not None:
            prepare(fd)
        for name in parts:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if prepare is not None:
                prepare(fd)
        yield fd
    finally:
        os.close(fd)


def workspace_parts(path: str) -> tuple[str, ...]:
    """Canonicalize virtual paths lexically, without touching an untrusted tree."""
    if "\x00" in path or ".." in PurePosixPath(path).parts:
        raise ValueError("file path may not contain .. or NUL")
    normalized = path.rstrip("/") or "/workspace"
    relative = "" if normalized == "/workspace" else normalized.removeprefix("/workspace/")
    return tuple(part for part in PurePosixPath(relative.lstrip("/")).parts if part != ".")


@contextlib.contextmanager
def _file_errors() -> Iterator[None]:
    """Report safe client errors, not privileged host pathnames."""
    try:
        yield
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise ValueError("path does not exist") from exc
        if exc.errno == errno.ENOTDIR:
            raise ValueError("path is not a directory or traverses a symlink") from exc
        if exc.errno == errno.ELOOP:
            raise ValueError("file path may not traverse symlinks") from exc
        if exc.errno == errno.EEXIST:
            raise ValueError("path already exists") from exc
        if exc.errno in {errno.ENOTEMPTY, errno.EBUSY}:
            raise ValueError("directory is not empty or is busy") from exc
        # Includes permission errors and special files that cannot be opened.
        raise ValueError("workspace operation could not be completed") from exc


class WorkspaceFiles:
    def __init__(self, workspace: Path, uid: int) -> None:
        self.workspace = workspace
        self.uid = uid

    def _secure(self, fd: int) -> None:
        os.chown(fd, self.uid, self.uid)
        os.fchmod(fd, 0o700)

    @contextlib.contextmanager
    def _directory(
        self,
        parts: tuple[str, ...],
        *,
        create: bool = False,
        secure: bool = False,
        label: str = "path",
    ) -> Iterator[int]:
        try:
            with open_directory(
                self.workspace, parts, create=create, prepare=self._secure if secure else None
            ) as fd:
                yield fd
        except FileNotFoundError as exc:
            prefix = "" if label == "path" else label + " "
            raise ValueError(f"{prefix}parent directory does not exist") from exc

    def read(self, path: str, limit: int) -> bytes:
        if limit < 1:
            raise ValueError("file size limit must be positive")
        parts = workspace_parts(path)
        if not parts:
            raise ValueError("path is not a regular file")
        with _file_errors(), self._directory(parts[:-1]) as parent:
            fd = os.open(
                parts[-1],
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=parent,
            )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("path is not a regular file")
                if info.st_size > limit:
                    raise ValueError("file exceeds the sandbox File API size limit")
                # The agent can grow the file after fstat. Bound the read too.
                content = stream.read(limit + 1)
                if len(content) > limit:
                    raise ValueError("file exceeds the sandbox File API size limit")
                return content

    def write(self, path: str, content: bytes) -> None:
        parts = workspace_parts(path)
        if not parts:
            raise ValueError("cannot write the workspace root")
        with _file_errors(), self._directory(parts[:-1], create=True, secure=True) as parent:
            # Do not follow even a final symlink. A replacement after this
            # check is safe: rename replaces the entry, not its referent.
            with contextlib.suppress(FileNotFoundError):
                info = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("path is not a regular file")
            temporary = f".sandbox-write-{uuid.uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.chown(stream.fileno(), self.uid, self.uid)
                    os.fchmod(stream.fileno(), 0o600)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent)

    def list(self, path: str, *, limit: int, offset: int) -> tuple[list[dict[str, Any]], int, bool]:
        parts = workspace_parts(path)
        with _file_errors(), self._directory(parts) as directory:
            with os.scandir(directory) as scan:
                names = sorted(entry.name for entry in scan)
            entries: list[dict[str, Any]] = []
            for name in names[offset : offset + limit]:
                try:
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # Agent commands may remove entries during a listing.
                link = stat.S_ISLNK(info.st_mode)
                kind = (
                    "symlink" if link else ("directory" if stat.S_ISDIR(info.st_mode) else "file")
                )
                entries.append(
                    {
                        "name": name,
                        "path": "/workspace/" + "/".join((*parts, name)),
                        "type": kind,
                        "size_bytes": info.st_size,
                        "modified_at": info.st_mtime,
                        "mode": stat.S_IMODE(info.st_mode),
                    }
                )
            return entries, len(names), offset + limit < len(names)

    def mkdir(self, path: str, *, parents: bool) -> None:
        parts = workspace_parts(path)
        if not parts:
            raise ValueError("cannot create the workspace root")
        with _file_errors(), self._directory(parts[:-1], create=parents, secure=True) as parent:
            try:
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent)
            except FileExistsError:
                if not parents:
                    raise
            fd = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                self._secure(fd)
            finally:
                os.close(fd)

    @staticmethod
    def _remove(parent: int, name: str, *, recursive: bool) -> None:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(name, dir_fd=parent)
        elif recursive:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ValueError("safe recursive deletion is unavailable on this platform")
            shutil.rmtree(name, dir_fd=parent)
        else:
            os.rmdir(name, dir_fd=parent)

    def delete(self, path: str, *, recursive: bool) -> None:
        parts = workspace_parts(path)
        if not parts:
            raise ValueError("cannot delete the workspace root")
        with _file_errors(), self._directory(parts[:-1]) as parent:
            self._remove(parent, parts[-1], recursive=recursive)

    def move(self, source: str, destination: str, *, overwrite: bool) -> None:
        src, dst = workspace_parts(source), workspace_parts(destination)
        if not src or not dst:
            raise ValueError("cannot move the workspace root")
        if src == dst:
            return
        with (
            _file_errors(),
            self._directory(src[:-1], label="source") as source_parent,
            self._directory(dst[:-1], secure=True, label="destination") as target_parent,
        ):
            try:
                os.stat(src[-1], dir_fd=source_parent, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise ValueError("source path does not exist") from exc
            try:
                target = os.stat(dst[-1], dir_fd=target_parent, follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target is not None:
                if not overwrite:
                    raise ValueError("destination already exists")
                if stat.S_ISDIR(target.st_mode):
                    # Moving a tree into itself must not delete the source.
                    if src[: len(dst)] == dst or dst[: len(src)] == src:
                        raise ValueError("cannot move overlapping directories")
                    self._remove(target_parent, dst[-1], recursive=True)
            os.replace(src[-1], dst[-1], src_dir_fd=source_parent, dst_dir_fd=target_parent)
