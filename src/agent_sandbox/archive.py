"""Bounded, descriptor-relative snapshots of agent-controlled POSIX trees.

Directory descriptors, not a prior pathname check, establish the read boundary.
Links are archived as links, never opened. Limits are cooperative: a blocked
filesystem syscall still requires an outer storage/worker timeout.
"""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import stat
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from .workspace import open_directory


class ArchiveError(RuntimeError):
    """A source changed, is unsupported, or exceeds the snapshot work budget."""


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_source_bytes: int = 8 * 1024 * 1024 * 1024
    max_archive_bytes: int | None = None
    max_entries: int = 200_000
    max_depth: int = 128
    timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_source_bytes < 1 or self.max_entries < 1:
            raise ValueError("snapshot size and entry limits must be positive")
        if self.max_archive_bytes is not None and self.max_archive_bytes < 1:
            raise ValueError("snapshot archive limit must be positive")
        if not 1 <= self.max_depth <= 256:
            raise ValueError("snapshot depth limit must be between 1 and 256")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("snapshot timeout must be positive and finite")


def _identity(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)


def _version(info: os.stat_result) -> tuple[int, int, int]:
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns


class _ArchiveOutput:
    def __init__(self, raw: BinaryIO, limits: ArchiveLimits, checkpoint: Callable[[], None]):
        self.raw = raw
        self.limits = limits
        self.checkpoint = checkpoint
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, content: bytes) -> int:
        self.checkpoint()
        maximum = self.limits.max_archive_bytes
        if maximum is not None and self.size + len(content) > maximum:
            raise ArchiveError(f"template archive is over the {maximum} byte limit")
        count = self.raw.write(content)
        self.digest.update(content[:count])
        self.size += count
        return count

    def flush(self) -> None:
        self.raw.flush()


class _SnapshotReader:
    def __init__(self, stream: BinaryIO, checkpoint: Callable[[], None]):
        self.stream = stream
        self.checkpoint = checkpoint

    def read(self, size: int) -> bytes:
        self.checkpoint()
        return self.stream.read(size)


class _Snapshot:
    def __init__(self, limits: ArchiveLimits):
        self.limits = limits
        self.deadline = time.monotonic() + limits.timeout_seconds
        self.entries = 0
        self.source_bytes = 0

    def checkpoint(self) -> None:
        if time.monotonic() >= self.deadline:
            raise ArchiveError("template snapshot exceeded its time limit")

    def walk(self, directory: int, prefix: str, depth: int, tar: tarfile.TarFile) -> None:
        self.checkpoint()
        names: list[str] = []
        with os.scandir(directory) as scan:
            for entry in scan:
                self.checkpoint()
                self.entries += 1
                if self.entries > self.limits.max_entries:
                    raise ArchiveError("template snapshot exceeded its entry limit")
                names.append(entry.name)
        for name in sorted(names):
            self.checkpoint()
            if depth > self.limits.max_depth:
                raise ArchiveError("template snapshot exceeded its depth limit")
            arcname = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            info = tarfile.TarInfo(arcname)
            info.mode = stat.S_IMODE(before.st_mode)
            # TarInfo defaults already zero timestamps and owner metadata.
            if stat.S_ISLNK(before.st_mode):
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(name, dir_fd=directory)
                after = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _identity(before) != _identity(after) or _version(before) != _version(after):
                    raise ArchiveError("template source changed during snapshot")
                tar.addfile(info)
            elif stat.S_ISDIR(before.st_mode):
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
                child = os.open(name, flags, dir_fd=directory)
                try:
                    if _identity(before) != _identity(os.fstat(child)):
                        raise ArchiveError("template source changed during snapshot")
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                    self.walk(child, arcname, depth + 1, tar)
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                self.source_bytes += before.st_size
                if self.source_bytes > self.limits.max_source_bytes:
                    raise ArchiveError("template snapshot exceeded its source byte limit")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                fd = os.open(name, flags, dir_fd=directory)
                with os.fdopen(fd, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if _identity(before) != _identity(opened) or _version(before) != _version(
                        opened
                    ):
                        raise ArchiveError("template source changed during snapshot")
                    info.size = opened.st_size
                    reader = _SnapshotReader(stream, self.checkpoint)
                    tar.addfile(info, cast(BinaryIO, reader))
                    if _version(opened) != _version(os.fstat(stream.fileno())):
                        raise ArchiveError("template source changed during snapshot")
            else:
                raise ArchiveError("template source contains an unsupported special file")


def snapshot_directory(
    source: Path | int, destination: Path, limits: ArchiveLimits
) -> tuple[str, int]:
    """Archive a pinned directory, or a path whose parent chain is trusted.

    Caller-owned descriptors remain open. Refuse source replacements rather
    than retrying reads against a different inode. This is not a transactional
    filesystem snapshot; callers must fence cooperating writers separately.
    """
    if isinstance(source, int):
        fd = os.dup(source)
    else:
        with open_directory(source) as directory:
            fd = os.dup(directory)
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise ArchiveError("template source is not a directory")
        snapshot = _Snapshot(limits)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("wb") as raw:
                output = _ArchiveOutput(raw, limits, snapshot.checkpoint)
                with gzip.GzipFile(
                    fileobj=cast(BinaryIO, output),
                    mode="wb",
                    mtime=0,
                    compresslevel=6,
                    filename="",
                ) as compressed:
                    with tarfile.open(
                        fileobj=compressed,
                        mode="w|",
                        format=tarfile.PAX_FORMAT,
                    ) as tar:
                        snapshot.walk(fd, "", 1, tar)
                snapshot.checkpoint()
            return f"sha256:{output.digest.hexdigest()}", output.size
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    finally:
        os.close(fd)
