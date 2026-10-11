"""Dormant workspace snapshots: what lets a suspended sandbox wake elsewhere.

A suspended sandbox keeps its directory on the worker that hosted it, and that
is enough to resume it there. With local storage, resuming anywhere else needs
the bytes, so a suspend can also archive the directories that hold state —
`/workspace`, `/home` and `/envs` — to the object store. `/cache` and the logs
are throwaway and are recreated empty.

Layout, under the store's base prefix:

    checkpoints/<sandbox_id>/dormant/manifest.json
    checkpoints/<sandbox_id>/dormant/<nonce>/<part>.tar.gz

The manifest is written last and names the nonce, so a snapshot is visible only
once every part is uploaded, and a failed snapshot never replaces a good one
with a half-written one. Parts are verified against the manifest's digests
before they are extracted.

This is a filesystem snapshot, not a process checkpoint: nothing running is
captured, and nothing is running when it is taken.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .archive import ArchiveError, ArchiveLimits, snapshot_directory
from .templates import TemplateError, TemplateObjectStore, digest_of_file, extract_archive
from .workspace import open_directory

logger = logging.getLogger(__name__)

# The sandbox directories that carry state across a suspend.
SNAPSHOT_PARTS = ("workspace", "home", "envs")
MANIFEST_VERSION = 1


def snapshot_prefix(sandbox_id: str) -> str:
    return f"checkpoints/{sandbox_id}/dormant"


def manifest_key(sandbox_id: str) -> str:
    return f"{snapshot_prefix(sandbox_id)}/manifest.json"


def part_key(sandbox_id: str, nonce: str, part: str) -> str:
    return f"{snapshot_prefix(sandbox_id)}/{nonce}/{part}.tar.gz"


def read_manifest(store: TemplateObjectStore, sandbox_id: str) -> dict[str, Any] | None:
    """The current snapshot's manifest, or None when there is no snapshot."""

    with tempfile.TemporaryDirectory() as scratch:
        destination = Path(scratch) / "manifest.json"
        try:
            store.download_to(manifest_key(sandbox_id), destination)
        except FileNotFoundError:
            return None
        try:
            data = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("dormant snapshot manifest is unreadable: sandbox_id=%s", sandbox_id)
            return None
    if not isinstance(data, dict) or data.get("sandbox_id") != sandbox_id:
        return None
    if not isinstance(data.get("nonce"), str) or not isinstance(data.get("parts"), dict):
        return None
    return data


def write_snapshot(
    store: TemplateObjectStore,
    *,
    sandbox_id: str,
    generation: int,
    root: Path,
    staging_root: Path,
    limits: ArchiveLimits,
) -> dict[str, Any]:
    """Archive the stateful directories of `root` and publish a new manifest.

    The caller fences every cooperating writer first; see `snapshot_directory`
    for why this is not an atomic filesystem snapshot.
    """

    previous = read_manifest(store, sandbox_id)
    nonce = uuid.uuid4().hex
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="dormant-", dir=staging_root))
    parts: dict[str, dict[str, Any]] = {}
    try:
        for part in SNAPSHOT_PARTS:
            if not (root / part).is_dir():
                continue
            archive = scratch / f"{part}.tar.gz"
            try:
                with open_directory(root, (part,)) as directory:
                    digest, size = snapshot_directory(directory, archive, limits)
            except (ArchiveError, OSError, ValueError) as exc:
                logger.warning(
                    "dormant snapshot failed: sandbox_id=%s part=%s error=%s",
                    sandbox_id,
                    part,
                    exc,
                )
                raise RuntimeError("SANDBOX_SNAPSHOT_FAILED") from exc
            store.upload_file(part_key(sandbox_id, nonce, part), archive)
            parts[part] = {"digest": digest, "size_bytes": size}
            archive.unlink(missing_ok=True)
        manifest = {
            "version": MANIFEST_VERSION,
            "sandbox_id": sandbox_id,
            "generation": generation,
            "nonce": nonce,
            "created_at": time.time(),
            "parts": parts,
        }
        document = scratch / "manifest.json"
        document.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        store.upload_file(manifest_key(sandbox_id), document)
    except BaseException:
        # Parts of a manifest nobody will ever publish.
        for part in parts:
            with contextlib.suppress(Exception):
                store.delete(part_key(sandbox_id, nonce, part))
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if previous is not None:
        _delete_parts(store, sandbox_id, previous)
    return manifest


def restore_snapshot(
    store: TemplateObjectStore,
    *,
    sandbox_id: str,
    destination: Path,
    uid: int,
    max_extract_bytes: int | None,
) -> dict[str, Any] | None:
    """Rebuild a sandbox tree at `destination` from the current snapshot.

    Returns the manifest, or None when there is no snapshot. `destination` must
    not exist; the caller renames it into place once this returns, so a failed
    restore never leaves a half-populated sandbox root behind.
    """

    manifest = read_manifest(store, sandbox_id)
    if manifest is None:
        return None
    destination.mkdir(mode=0o700, parents=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as scratch:
        for part, details in sorted(manifest["parts"].items()):
            if part not in SNAPSHOT_PARTS or not isinstance(details, dict):
                raise RuntimeError("SANDBOX_SNAPSHOT_FAILED")
            archive = Path(scratch) / f"{part}.tar.gz"
            store.download_to(part_key(sandbox_id, manifest["nonce"], part), archive)
            if digest_of_file(archive) != details.get("digest"):
                logger.warning(
                    "dormant snapshot digest mismatch: sandbox_id=%s part=%s", sandbox_id, part
                )
                raise RuntimeError("SANDBOX_SNAPSHOT_FAILED")
            try:
                # The template extractor already refuses traversal, escaping
                # links, and device nodes, and strips setuid bits.
                extract_archive(archive, destination / part, max_bytes=max_extract_bytes)
            except TemplateError as exc:
                raise RuntimeError("SANDBOX_SNAPSHOT_FAILED") from exc
            archive.unlink(missing_ok=True)
    for name in ("workspace", "home", "cache", "envs", "logs"):
        (destination / name).mkdir(exist_ok=True)
    chown_tree(destination, uid)
    return manifest


def chown_tree(root: Path, uid: int) -> None:
    """Hand a restored tree to the sandbox UID without following any link.

    Extraction runs as the service user, and a symlink in agent-controlled
    content may point anywhere on the host: `lchown` changes the link itself.
    """

    os.chown(root, uid, uid, follow_symlinks=False)
    for current, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            os.chown(os.path.join(current, name), uid, uid, follow_symlinks=False)


def delete_snapshot(store: TemplateObjectStore, sandbox_id: str) -> bool:
    """Remove the current snapshot, manifest first. Returns whether one existed."""

    manifest = read_manifest(store, sandbox_id)
    if manifest is None:
        return False
    store.delete(manifest_key(sandbox_id))
    _delete_parts(store, sandbox_id, manifest)
    return True


def _delete_parts(store: TemplateObjectStore, sandbox_id: str, manifest: dict[str, Any]) -> None:
    for part in manifest.get("parts", {}):
        try:
            store.delete(part_key(sandbox_id, str(manifest["nonce"]), str(part)))
        except Exception:
            logger.warning(
                "could not delete a superseded dormant snapshot part: sandbox_id=%s part=%s",
                sandbox_id,
                part,
                exc_info=True,
            )


__all__ = [
    "SNAPSHOT_PARTS",
    "chown_tree",
    "delete_snapshot",
    "manifest_key",
    "part_key",
    "read_manifest",
    "restore_snapshot",
    "snapshot_prefix",
    "write_snapshot",
]
