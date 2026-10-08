"""Environment template behavior.

The feature's value rests on three claims: an identical tree always produces an
identical digest (or the cache is useless), a hostile archive cannot escape its
destination (or a shared template store is a vulnerability), and a half-finished
materialization is never visible (or a sandbox mounts a partial environment).
Each is tested directly.
"""

from __future__ import annotations

import os
import re
import stat
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_sandbox.backends import as_template_cache_pruning
from agent_sandbox.config import Settings
from agent_sandbox.runtime import SandboxRuntime
from agent_sandbox.templates import (
    TEMPLATE_MOUNT_ROOT,
    LocalTemplateCache,
    TemplateCatalog,
    TemplateError,
    TemplateManager,
    TemplateRecord,
    TemplateRef,
    archive_directory,
    as_template_object_store,
    digest_of_file,
    extract_archive,
    object_key_for,
    template_object_store,
    validate_template_name,
)


def _make_tree(root: Path) -> Path:
    """A tree with the features a real environment has: nesting, modes, symlinks."""
    (root / "lib" / "python3.13" / "site-packages").mkdir(parents=True)
    (root / "bin").mkdir()
    (root / "bin" / "python").write_text("#!/bin/sh\nexec python3\n")
    os.chmod(root / "bin" / "python", 0o755)
    (root / "lib" / "python3.13" / "site-packages" / "mod.py").write_text("VALUE = 1\n")
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (root / "bin" / "python3").symlink_to("python")
    return root


# --- Identity ---------------------------------------------------------------


def test_identical_trees_produce_identical_digests(tmp_path: Path) -> None:
    """Without this the digest is not a cache key and nothing else works."""
    first = _make_tree(tmp_path / "a")
    second = _make_tree(tmp_path / "b")

    digest_a, _ = archive_directory(first, tmp_path / "a.tar.gz")
    digest_b, _ = archive_directory(second, tmp_path / "b.tar.gz")

    assert digest_a == digest_b


def test_digest_is_stable_across_mtimes(tmp_path: Path) -> None:
    """Rebuilding the same dependency set must not invalidate the cache."""
    tree = _make_tree(tmp_path / "env")
    digest_before, _ = archive_directory(tree, tmp_path / "before.tar.gz")

    os.utime(tree / "pyvenv.cfg", (1_000_000, 1_000_000))
    os.utime(tree / "bin" / "python", (2_000_000, 2_000_000))
    digest_after, _ = archive_directory(tree, tmp_path / "after.tar.gz")

    assert digest_before == digest_after


def test_changed_content_changes_the_digest(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path / "env")
    digest_before, _ = archive_directory(tree, tmp_path / "before.tar.gz")

    (tree / "lib" / "python3.13" / "site-packages" / "mod.py").write_text("VALUE = 2\n")
    digest_after, _ = archive_directory(tree, tmp_path / "after.tar.gz")

    assert digest_before != digest_after


def test_round_trip_preserves_modes_and_symlinks(tmp_path: Path) -> None:
    """A venv whose entry points lost their executable bit is not usable."""
    tree = _make_tree(tmp_path / "env")
    archive_directory(tree, tmp_path / "env.tar.gz")
    destination = tmp_path / "restored"

    extract_archive(tmp_path / "env.tar.gz", destination)

    assert (destination / "bin" / "python").stat().st_mode & 0o111
    assert (destination / "bin" / "python3").is_symlink()
    assert os.readlink(destination / "bin" / "python3") == "python"
    assert (destination / "lib" / "python3.13" / "site-packages" / "mod.py").read_text()


# --- Refs and naming --------------------------------------------------------


def test_ref_parses_a_bare_name() -> None:
    ref = TemplateRef.parse("python-ml")

    assert ref.name == "python-ml"
    assert ref.digest is None
    assert ref.mount_target == "/envs/python-ml"


def test_ref_parses_a_pinned_digest() -> None:
    digest = "sha256:" + "a" * 64

    ref = TemplateRef.parse(f"python-ml@{digest}")

    assert ref.digest == digest
    assert str(ref) == f"python-ml@{digest}"


@pytest.mark.parametrize("name", [".", ".."])
def test_a_name_that_is_a_path_segment_is_refused_by_name(name: str) -> None:
    """`..` as a template name would be a directory traversal, not a template.

    The message names the value rather than listing what is allowed, because
    this is the one input that is a path rather than a typo.
    """

    with pytest.raises(TemplateError, match=rf"invalid template name '{re.escape(name)}'"):
        validate_template_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "Upper",
        "",
        ".",
        "..",
        "with space",
        "a" * 65,
    ],
)
def test_invalid_names_are_rejected(name: str) -> None:
    """A name becomes a path segment and an object key, so it must be strict."""
    with pytest.raises(TemplateError):
        validate_template_name(name)


def test_invalid_digest_is_rejected() -> None:
    with pytest.raises(TemplateError):
        TemplateRef.parse("env@sha256:short")


def test_object_key_embeds_the_digest() -> None:
    """Keys must be immutable so a cached object is provably the right one."""
    key = object_key_for("python-ml", "sha256:" + "b" * 64)

    assert key == f"templates/python-ml/sha256-{'b' * 64}.tar.gz"


# --- Archive safety ---------------------------------------------------------


def _write_hostile_archive(path: Path, member_name: str, *, link_to: str | None = None) -> None:
    with tarfile.open(path, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        if link_to is not None:
            info.type = tarfile.SYMTYPE
            info.linkname = link_to
            tar.addfile(info)
        else:
            payload = b"pwned"
            info.size = len(payload)
            import io

            tar.addfile(info, io.BytesIO(payload))


def test_archive_with_a_traversal_path_is_rejected(tmp_path: Path) -> None:
    """Templates are shared between tenants, so extraction is a trust boundary."""
    archive = tmp_path / "hostile.tar.gz"
    _write_hostile_archive(archive, "../escaped.txt")

    with pytest.raises(TemplateError, match="escapes its destination"):
        extract_archive(archive, tmp_path / "destination")

    assert not (tmp_path / "escaped.txt").exists()


def test_archive_with_an_absolute_path_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "hostile.tar.gz"
    _write_hostile_archive(archive, "/etc/passwd")

    with pytest.raises(TemplateError, match="absolute path"):
        extract_archive(archive, tmp_path / "destination")


def test_archive_with_an_escaping_relative_symlink_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "hostile.tar.gz"
    _write_hostile_archive(archive, "link", link_to="../../../../etc/passwd")

    with pytest.raises(TemplateError, match="link escaping"):
        extract_archive(archive, tmp_path / "destination")


def test_absolute_symlinks_are_preserved(tmp_path: Path) -> None:
    """Every virtualenv has one, and it is not an escape.

    `bin/python3.13 -> /usr/local/bin/python3.13` is resolved by the sandbox in
    its own mount namespace, where /usr is the read-only base image. Rejecting
    it would reject essentially every real Python environment; preserving it is
    what makes a template runnable.
    """
    tree = tmp_path / "env"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "python3.13").symlink_to("/usr/local/bin/python3.13")
    archive_directory(tree, tmp_path / "env.tar.gz")
    destination = tmp_path / "restored"

    extract_archive(tmp_path / "env.tar.gz", destination)

    link = destination / "bin" / "python3.13"
    assert link.is_symlink()
    assert os.readlink(link) == "/usr/local/bin/python3.13"


def test_archive_with_an_absolute_hard_link_is_rejected(tmp_path: Path) -> None:
    """Unlike a symlink, a hard link is resolved against the host at extraction."""
    archive = tmp_path / "hostile.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("shadow")
        info.type = tarfile.LNKTYPE
        info.linkname = "/etc/shadow"
        tar.addfile(info)

    with pytest.raises(TemplateError, match="absolute hard link"):
        extract_archive(archive, tmp_path / "destination")


def test_archive_with_an_escaping_hard_link_is_rejected(tmp_path: Path) -> None:
    """The relative form of the same escape, which resolves against the host.

    A hard link is materialized by the extractor rather than stored as a link,
    so `../../../../etc/shadow` would have put a real file's contents into the
    template — and from there into every sandbox that mounts it.
    """
    archive = tmp_path / "hostile.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("shadow")
        info.type = tarfile.LNKTYPE
        info.linkname = "../../../../etc/shadow"
        tar.addfile(info)

    with pytest.raises(TemplateError, match="link escaping its destination"):
        extract_archive(archive, tmp_path / "destination")


@pytest.mark.parametrize(
    ("entry_type", "label"),
    [(tarfile.FIFOTYPE, "fifo"), (tarfile.CHRTYPE, "device")],
)
def test_archive_with_a_fifo_or_device_entry_is_rejected(
    tmp_path: Path, entry_type: bytes, label: str
) -> None:
    """Neither can be built here, so both have to be refused on the way in.

    `archive_directory` will not produce one, which leaves an archive written by
    something else as the only way one arrives — and a shared object store is
    exactly the place that can happen. A FIFO in a cache tree blocks a reader
    forever; a device node with the right major number is worse.
    """
    archive = tmp_path / f"{label}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(f"env/{label}")
        info.type = entry_type
        info.devmajor, info.devminor = 1, 3
        tar.addfile(info)

    with pytest.raises(TemplateError, match="device or FIFO entry"):
        extract_archive(archive, tmp_path / "destination")


def test_building_from_a_file_is_refused(tmp_path: Path) -> None:
    """A source that is not a directory is a caller mistake, not an empty tree."""

    source = tmp_path / "requirements.txt"
    source.write_text("numpy\n")

    with pytest.raises(TemplateError, match="template source is not a directory"):
        archive_directory(source, tmp_path / "env.tar.gz")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo is a POSIX call")
def test_building_from_a_special_file_is_refused(tmp_path: Path) -> None:
    """The build side of the FIFO rule: what cannot be built cannot ship."""

    tree = _make_tree(tmp_path / "env")
    os.mkfifo(tree / "pipe")

    with pytest.raises(TemplateError, match="unsupported special file"):
        archive_directory(tree, tmp_path / "env.tar.gz")


def test_extraction_is_bounded(tmp_path: Path) -> None:
    """A small archive can expand arbitrarily; the cache disk is shared."""
    tree = _make_tree(tmp_path / "env")
    archive_directory(tree, tmp_path / "env.tar.gz")

    with pytest.raises(TemplateError, match="refusing to extract"):
        extract_archive(tmp_path / "env.tar.gz", tmp_path / "destination", max_bytes=8)


# --- Cache ------------------------------------------------------------------


def test_materialize_extracts_once_and_reuses(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache = LocalTemplateCache(tmp_path / "cache")
    calls = 0

    def fetch(destination: Path) -> None:
        nonlocal calls
        calls += 1
        destination.write_bytes(archive.read_bytes())

    first = cache.materialize("env", digest, fetch)
    second = cache.materialize("env", digest, fetch)

    assert first == second
    assert calls == 1, "a cached template must not be fetched again"
    assert (first / "pyvenv.cfg").exists()


def test_two_materializations_of_one_revision_do_not_both_extract(tmp_path: Path) -> None:
    """The lock makes the second caller wait, and the wait has to pay off.

    Two processes sharing a template root is a supported layout, and the
    expensive half is the download and extraction, not the lookup. Without the
    re-check under the lock the loser of the race would fetch and extract the
    same revision a second time, next to the tree it can already see.
    """
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache = LocalTemplateCache(tmp_path / "cache")
    calls: list[float] = []
    fetched = threading.Event()

    def fetch(destination: Path) -> None:
        # Hold the lock long enough for the second thread to be waiting on it,
        # so the branch this test exists for is actually reached.
        fetched.set()
        time.sleep(0.2)
        calls.append(time.monotonic())
        destination.write_bytes(archive.read_bytes())

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.materialize, "env", digest, fetch)
        assert fetched.wait(timeout=5), "the first materialization never started"
        second = pool.submit(cache.materialize, "env", digest, fetch)
        paths = {first.result(timeout=30), second.result(timeout=30)}

    assert paths == {cache.path_for("env", digest)}
    assert len(calls) == 1, "the revision was fetched once for two callers"


def test_a_fetch_that_produces_nothing_is_an_error(tmp_path: Path) -> None:
    """A store that answers with no bytes is broken, not an empty template."""

    cache = LocalTemplateCache(tmp_path / "cache")

    with pytest.raises(TemplateError, match="produced no archive"):
        cache.materialize("env", "sha256:" + "d" * 64, lambda destination: None)


def test_an_unset_budget_never_evicts(tmp_path: Path) -> None:
    """The default. `SANDBOX_TEMPLATE_CACHE_MAX_BYTES` unset means keep it all."""

    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache = LocalTemplateCache(tmp_path / "cache")
    cache.materialize("env", digest, lambda d: d.write_bytes(archive.read_bytes()))

    assert cache.prune() == []
    assert cache.has("env", digest)


def test_a_cache_within_its_budget_is_left_alone(tmp_path: Path) -> None:
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache = LocalTemplateCache(tmp_path / "cache", max_total_bytes=2**30)
    cache.materialize("env", digest, lambda d: d.write_bytes(archive.read_bytes()))

    assert cache.prune() == []
    assert cache.has("env", digest)


def test_pruning_an_empty_or_absent_cache_is_not_an_error(tmp_path: Path) -> None:
    """The sweep runs on a timer, so it runs before anything is cached."""

    assert LocalTemplateCache(tmp_path / "absent", max_total_bytes=1).prune() == []
    assert LocalTemplateCache(tmp_path / "empty", max_total_bytes=1).prune() == []


def test_a_stray_file_in_the_cache_is_not_a_revision(tmp_path: Path) -> None:
    """The layout is scanned, not indexed, so anything else has to be skipped."""

    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    root = tmp_path / "cache"
    cache = LocalTemplateCache(root, max_total_bytes=1)
    cache.materialize("env", digest, lambda d: d.write_bytes(archive.read_bytes()))
    (root / "env" / "stray.txt").write_text("not a revision\n")

    evicted = cache.prune()

    assert evicted == [("env", digest)]
    assert (root / "env" / "stray.txt").exists(), "the sweep removed something it does not own"


def test_removing_a_revision_that_is_not_there_is_not_an_error(tmp_path: Path) -> None:
    cache = LocalTemplateCache(tmp_path / "cache")

    assert cache.remove("env", "sha256:" + "e" * 64) is False


def test_a_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    """The digest is the only guarantee that a fetched archive is the right one."""
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    archive_directory(tree, archive)
    cache = LocalTemplateCache(tmp_path / "cache")
    wrong_digest = "sha256:" + "c" * 64

    with pytest.raises(TemplateError, match="digest mismatch"):
        cache.materialize("env", wrong_digest, lambda d: d.write_bytes(archive.read_bytes()))

    assert not cache.has("env", wrong_digest)


def test_a_failed_fetch_leaves_no_partial_tree(tmp_path: Path) -> None:
    """A partially extracted environment must never be mountable."""
    cache = LocalTemplateCache(tmp_path / "cache")
    digest = "sha256:" + "d" * 64

    def failing_fetch(destination: Path) -> None:
        destination.write_bytes(b"truncated")
        raise OSError("network died")

    with pytest.raises(OSError, match="network died"):
        cache.materialize("env", digest, failing_fetch)

    assert not cache.has("env", digest)
    assert not any(cache.staging_root.iterdir()) if cache.staging_root.exists() else True


def test_revisions_of_one_name_coexist(tmp_path: Path) -> None:
    """Rollback must be a remount, not a rebuild."""
    cache = LocalTemplateCache(tmp_path / "cache")
    first_tree = _make_tree(tmp_path / "v1")
    first_archive = tmp_path / "v1.tar.gz"
    first_digest, _ = archive_directory(first_tree, first_archive)

    second_tree = _make_tree(tmp_path / "v2")
    (second_tree / "extra.py").write_text("NEW = True\n")
    second_archive = tmp_path / "v2.tar.gz"
    second_digest, _ = archive_directory(second_tree, second_archive)

    cache.materialize("env", first_digest, lambda d: d.write_bytes(first_archive.read_bytes()))
    cache.materialize("env", second_digest, lambda d: d.write_bytes(second_archive.read_bytes()))

    assert cache.has("env", first_digest)
    assert cache.has("env", second_digest)
    assert not (cache.path_for("env", first_digest) / "extra.py").exists()
    assert (cache.path_for("env", second_digest) / "extra.py").exists()


def test_prune_evicts_least_recently_used(tmp_path: Path) -> None:
    cache = LocalTemplateCache(tmp_path / "cache", max_total_bytes=1)
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache.materialize("env", digest, lambda d: d.write_bytes(archive.read_bytes()))

    evicted = cache.prune()

    assert ("env", digest) in evicted
    assert not cache.has("env", digest)


def test_prune_never_evicts_a_pinned_revision(tmp_path: Path) -> None:
    """Removing the source of a live bind mount would break running sandboxes."""
    cache = LocalTemplateCache(tmp_path / "cache", max_total_bytes=1)
    tree = _make_tree(tmp_path / "env")
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(tree, archive)
    cache.materialize("env", digest, lambda d: d.write_bytes(archive.read_bytes()))

    evicted = cache.prune(pinned=[("env", digest)])

    assert evicted == []
    assert cache.has("env", digest)


# --- Reclamation on a worker ------------------------------------------------


def _runtime_caching_two_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SandboxRuntime, str]:
    """A runtime whose cache holds one revision under two names.

    Two names so one can be attached to a sandbox and the other left free, with
    a budget of one byte so every prune call has to evict something. The
    revision is already in the cache, so attaching it needs no object store.
    """
    monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    settings = Settings(
        internal_token="token",
        local_root=tmp_path,
        min_free_bytes=0,
        disk_high_watermark_percent=99,
        template_cache_max_bytes=1,
        template_root=tmp_path / "templates",
    )
    runtime = SandboxRuntime(settings)
    archive = tmp_path / "env.tar.gz"
    digest, _ = archive_directory(_make_tree(tmp_path / "source"), archive)
    for name in ("env", "other"):
        runtime.templates.cache.materialize(
            name, digest, lambda destination: destination.write_bytes(archive.read_bytes())
        )
    return runtime, digest


def _record(name: str, digest: str) -> TemplateRecord:
    return TemplateRecord(
        name=name,
        digest=digest,
        size_bytes=1,
        mount_target=f"{TEMPLATE_MOUNT_ROOT}/{name}",
        created_at=0.0,
    )


def test_a_backend_without_template_cache_pruning_is_reported() -> None:
    """A plugin written against an earlier release must still load."""

    class OldBackend:
        async def cleanup_trash(self) -> None:
            return None

    assert as_template_cache_pruning(OldBackend()) is None
    assert as_template_cache_pruning(None) is None


async def test_pruning_spares_the_revision_a_live_sandbox_has_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache budget is only real if the worker enforces it, and the pin is
    what keeps enforcement from deleting a tree bind-mounted into a sandbox."""
    runtime, digest = _runtime_caching_two_names(tmp_path, monkeypatch)
    await runtime.create("sb-1", 1, os.getuid())
    runtime.attach_templates("sb-1", [_record("env", digest)])

    evicted = await runtime.prune_template_cache()

    assert evicted == [("other", digest)]
    assert runtime.templates.cache.has("env", digest)
    assert runtime.pinned_templates() == {("env", digest)}


async def test_destroying_a_sandbox_releases_its_revision_to_pruning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pin that outlives its sandbox is one nothing can ever evict, so every
    revision this worker attached would stay on disk for its lifetime."""
    runtime, digest = _runtime_caching_two_names(tmp_path, monkeypatch)
    await runtime.create("sb-1", 1, os.getuid())
    runtime.attach_templates("sb-1", [_record("env", digest)])

    await runtime.destroy("sb-1")

    assert runtime.pinned_templates() == set()
    # Both names are evictable now, and the budget is one byte.
    assert set(await runtime.prune_template_cache()) == {("env", digest), ("other", digest)}
    assert not runtime.templates.cache.has("env", digest)


# --- Manager ----------------------------------------------------------------


class _RecordingStore:
    """An object store double that records traffic, to assert on cache hits.

    It raises `FileNotFoundError` for an absent object because that is the
    interface the protocol documents, and the difference matters: this double
    used to raise `KeyError`, and the catalog used to swallow every exception
    alike, so a real store that could not download at all was indistinguishable
    from an empty catalog. Modelling absence the documented way is what lets the
    protocol be checked against the real implementation.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads = 0

    def upload_file(self, key: str, path: str | Path) -> str:
        self.objects[key] = Path(path).read_bytes()
        return f"s3://bucket/{key}"

    def download_to(self, key_or_uri: str, destination: str | Path) -> None:
        self.downloads += 1
        try:
            payload = self.objects[key_or_uri]
        except KeyError as error:
            raise FileNotFoundError(key_or_uri) from error
        Path(destination).write_bytes(payload)

    def delete(self, key_or_uri: str) -> None:
        self.objects.pop(key_or_uri, None)


def test_build_uploads_and_seeds_the_local_cache(tmp_path: Path) -> None:
    """The worker that built a template must never re-download it."""
    store = _RecordingStore()
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=store)
    tree = _make_tree(tmp_path / "env")

    record = manager.build(name="python-ml", source=tree, description="NumPy and friends")

    assert record.object_key in store.objects
    assert manager.cache.has(record.name, record.digest)
    manager.materialize(record)
    assert store.downloads == 0, "a locally built template must not be re-fetched"


def test_build_digest_matches_the_uploaded_bytes(tmp_path: Path) -> None:
    store = _RecordingStore()
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=store)
    tree = _make_tree(tmp_path / "env")

    record = manager.build(name="env", source=tree)

    uploaded = tmp_path / "uploaded.tar.gz"
    uploaded.write_bytes(store.objects[record.object_key])
    assert digest_of_file(uploaded) == record.digest


def test_a_second_worker_fetches_from_the_object_store(tmp_path: Path) -> None:
    """This is what makes a template reusable across a fleet."""
    store = _RecordingStore()
    builder = TemplateManager(LocalTemplateCache(tmp_path / "worker-a"), object_store=store)
    tree = _make_tree(tmp_path / "env")
    record = builder.build(name="env", source=tree)

    consumer = TemplateManager(LocalTemplateCache(tmp_path / "worker-b"), object_store=store)
    path = consumer.materialize(record)

    assert store.downloads == 1
    assert (path / "pyvenv.cfg").exists()
    consumer.materialize(record)
    assert store.downloads == 1, "the second use must hit the local cache"


def test_materializing_without_an_object_store_fails_clearly(tmp_path: Path) -> None:
    """Failing closed beats mounting an empty directory and confusing the user."""
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"))
    record = TemplateRecord(
        name="env",
        digest="sha256:" + "e" * 64,
        size_bytes=10,
        mount_target="/envs/env",
        created_at=0.0,
    )

    with pytest.raises(TemplateError, match="no object store is configured"):
        manager.materialize(record)


def test_conflicting_mount_targets_are_rejected(tmp_path: Path) -> None:
    """Two templates at one path would silently shadow each other."""
    store = _RecordingStore()
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=store)
    first = manager.build(name="a", source=_make_tree(tmp_path / "a"), mount_target="/envs/shared")
    second = manager.build(name="b", source=_make_tree(tmp_path / "b"), mount_target="/envs/shared")

    with pytest.raises(TemplateError, match="both mount at"):
        manager.resolve([first, second])


def test_the_conflict_names_both_revisions_not_just_the_name(tmp_path: Path) -> None:
    """Two revisions of one name collide on the mount target.

    Reporting "demo and demo" leaves the caller unable to tell which two they
    pinned, so the message has to carry the digests.
    """
    store = _RecordingStore()
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=store)
    source = _make_tree(tmp_path / "demo")
    old = manager.build(name="demo", source=source)
    (source / "marker.txt").write_text("changed\n")
    new = manager.build(name="demo", source=source)
    assert old.digest != new.digest

    with pytest.raises(TemplateError) as caught:
        manager.resolve([old, new])

    message = str(caught.value)
    assert old.digest in message
    assert new.digest in message
    assert message.count("demo@") == 2


def test_listing_the_same_revision_twice_mounts_it_once(tmp_path: Path) -> None:
    """A repeated template is a union artefact, not a conflict.

    Callers build the list by merging capability sets, so the same name shows up
    more than once. Refusing that turns a correct request into an error; the
    environment still mounts exactly once.
    """
    store = _RecordingStore()
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), object_store=store)
    record = manager.build(name="demo", source=_make_tree(tmp_path / "demo"))

    resolved = manager.resolve([record, record])

    assert [item.name for item, _ in resolved] == ["demo"]
    assert resolved[0][1] == manager.cache.path_for(record.name, record.digest)


def test_build_rejects_an_oversized_archive(tmp_path: Path) -> None:
    manager = TemplateManager(LocalTemplateCache(tmp_path / "cache"), max_archive_bytes=1)

    with pytest.raises(TemplateError, match="over the 1 byte limit"):
        manager.build(name="env", source=_make_tree(tmp_path / "env"))


def test_record_round_trips_through_a_dict(tmp_path: Path) -> None:
    """The record crosses the API boundary, so its serialization is contractual."""
    record = TemplateRecord(
        name="env",
        digest="sha256:" + "f" * 64,
        size_bytes=42,
        mount_target="/envs/env",
        created_at=1.5,
        description="test",
        source_sandbox_id="sandbox-a",
        labels={"language": "python"},
    )

    data = record.as_dict()

    assert data["name"] == "env"
    assert data["labels"] == {"language": "python"}
    assert data["mount_target"] == "/envs/env"


# --- Catalog ----------------------------------------------------------------


def test_catalog_resolves_a_name_to_the_current_revision(tmp_path: Path) -> None:
    catalog = TemplateCatalog(tmp_path / "catalog.json")
    record = TemplateRecord(
        name="env",
        digest="sha256:" + "1" * 64,
        size_bytes=10,
        mount_target="/envs/env",
        created_at=1.0,
    )
    catalog.publish(record)

    resolved = catalog.resolve(TemplateRef.parse("env"))

    assert resolved.digest == record.digest


def test_catalog_publish_moves_a_name_to_a_new_revision(tmp_path: Path) -> None:
    catalog = TemplateCatalog(tmp_path / "catalog.json")
    first = TemplateRecord(
        name="env",
        digest="sha256:" + "1" * 64,
        size_bytes=1,
        mount_target="/envs/env",
        created_at=1.0,
    )
    second = TemplateRecord(
        name="env",
        digest="sha256:" + "2" * 64,
        size_bytes=2,
        mount_target="/envs/env",
        created_at=2.0,
    )
    catalog.publish(first)
    catalog.publish(second)

    assert catalog.resolve(TemplateRef.parse("env")).digest == second.digest


def test_catalog_honors_a_pinned_digest(tmp_path: Path) -> None:
    """A pin must keep resolving to the old revision after the name moves on."""
    catalog = TemplateCatalog(tmp_path / "catalog.json")
    old_digest = "sha256:" + "1" * 64
    catalog.publish(
        TemplateRecord(
            name="env", digest=old_digest, size_bytes=1, mount_target="/envs/env", created_at=1.0
        )
    )
    catalog.publish(
        TemplateRecord(
            name="env",
            digest="sha256:" + "2" * 64,
            size_bytes=2,
            mount_target="/envs/env",
            created_at=2.0,
        )
    )

    resolved = catalog.resolve(TemplateRef.parse(f"env@{old_digest}"))

    assert resolved.digest == old_digest


def test_catalog_rejects_an_unknown_name(tmp_path: Path) -> None:
    catalog = TemplateCatalog(tmp_path / "catalog.json")

    with pytest.raises(TemplateError, match="not in the catalog"):
        catalog.resolve(TemplateRef.parse("missing"))


def test_catalog_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    TemplateCatalog(path).publish(
        TemplateRecord(
            name="env",
            digest="sha256:" + "3" * 64,
            size_bytes=1,
            mount_target="/envs/env",
            created_at=1.0,
        )
    )

    reopened = TemplateCatalog(path)

    assert reopened.get("env") is not None
    assert [record.name for record in reopened.list()] == ["env"]


def test_catalog_remove_makes_a_name_unresolvable(tmp_path: Path) -> None:
    catalog = TemplateCatalog(tmp_path / "catalog.json")
    catalog.publish(
        TemplateRecord(
            name="env",
            digest="sha256:" + "4" * 64,
            size_bytes=1,
            mount_target="/envs/env",
            created_at=1.0,
        )
    )

    assert catalog.remove("env") is True
    assert catalog.remove("env") is False
    assert catalog.get("env") is None


def test_catalog_is_shared_through_the_object_store(tmp_path: Path) -> None:
    """Publishing on one worker must make the name resolvable on another."""
    store = _RecordingStore()
    publisher = TemplateCatalog(tmp_path / "a" / "catalog.json", object_store=store)
    publisher.publish(
        TemplateRecord(
            name="env",
            digest="sha256:" + "5" * 64,
            size_bytes=1,
            mount_target="/envs/env",
            created_at=1.0,
        )
    )

    consumer = TemplateCatalog(tmp_path / "b" / "catalog.json", object_store=store)

    assert consumer.get("env") is not None


def test_catalog_tolerates_a_missing_shared_copy(tmp_path: Path) -> None:
    """Before the first publish there is no shared catalog; that is not an error."""
    catalog = TemplateCatalog(tmp_path / "catalog.json", object_store=_RecordingStore())

    assert catalog.list() == []


def test_corrupt_catalog_fails_loudly(tmp_path: Path) -> None:
    """Silently treating a corrupt catalog as empty would erase every name."""
    path = tmp_path / "catalog.json"
    path.write_text("{not json")

    with pytest.raises(TemplateError, match="unreadable"):
        TemplateCatalog(path).list()


# --- the real store has to satisfy the same protocol -------------------------


def test_a_broken_store_is_not_mistaken_for_an_empty_catalog(tmp_path: Path) -> None:
    """The failure this class of bug hid behind.

    A store that raises anything other than "not found" is broken, and saying
    `no templates yet` about it is how a download that never worked stays
    invisible for a whole release.
    """

    class _BrokenStore(_RecordingStore):
        def download_to(self, key_or_uri: str, destination: str | Path) -> None:
            raise AttributeError("something is wrong with this store")

    catalog = TemplateCatalog(tmp_path / "catalog.json", object_store=_BrokenStore())

    with pytest.raises(AttributeError):
        catalog.list()


def test_the_s3_store_satisfies_the_template_protocol(tmp_path: Path) -> None:
    """The double was correct and the implementation was not.

    `as_template_object_store` checks that the methods exist, not that they
    accept what the protocol says they accept, so a store whose `download_to`
    takes an open file instead of a path is accepted and then fails on the
    first fetch. This asserts the shape against the implementation that ships.
    """

    import inspect

    from agent_sandbox.blobstore import BlobStore

    parameters = [
        parameter
        for name, parameter in inspect.signature(BlobStore.download_to).parameters.items()
        if name != "self"
    ]
    destination = parameters[1]

    assert destination.name in {"destination", "target"}, (
        f"the destination is the second argument; found {destination.name!r}"
    )
    # The protocol hands a path, so a store that only accepts a stream fails
    # at the call site with an AttributeError from deep inside the store.
    assert "Path" in str(destination.annotation) or "str" in str(destination.annotation), (
        "BlobStore.download_to must accept a path, which is what the template "
        f"protocol passes; it takes {destination.annotation}"
    )


def test_a_store_without_the_streaming_methods_narrows_to_none() -> None:
    """A checkpoint-only store keeps templates worker-local rather than failing."""

    class _BytesOnly:
        def put(self, key: str, data: bytes) -> str:
            return key

        def get(self, key: str) -> bytes:
            return b""

        def delete(self, key: str) -> None:
            return None

    assert as_template_object_store(_BytesOnly()) is None
    assert as_template_object_store(None) is None


def test_the_sharing_channel_needs_a_bucket(tmp_path: Path) -> None:
    """No bucket means no sharing; templates stay on the worker that built them."""

    assert template_object_store(Settings(internal_token="t", blobstore_bucket="")) is None

    store = template_object_store(
        Settings(
            internal_token="t",
            blobstore_bucket="bucket",
            blobstore_endpoint="http://s3.test",
            object_store_backend="s3",
        )
    )
    assert store is not None


def test_both_halves_of_sharing_are_wired_in_one_place() -> None:
    """The catalog and the archive transfer must not be wired separately.

    They were. The catalog was given the object store and the template manager
    was not, so publishing a template uploaded a name that every worker could
    resolve and an archive that stayed on one — and the consumer's error was
    `template is not in the catalog`, which is what an unknown name says too.

    The wiring is asserted rather than trusted because a half-wired store fails
    silently in both directions: nothing errors when a template is built, and
    nothing errors on the worker that cannot fetch it beyond a message that
    reads like a typo.
    """

    import agent_sandbox.runtime as runtime_module
    import agent_sandbox.service as service_module

    for module in (runtime_module, service_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "template_object_store(" in source, (
            f"{module.__name__} does not route through the shared helper, so its "
            "object store can drift from the other half of the feature"
        )
        assert "as_template_object_store(" not in source, (
            f"{module.__name__} narrows an object store itself instead of using "
            "template_object_store(), which is how the two halves came apart"
        )


def test_materialize_grants_traversal_and_keeps_the_modes_a_tree_had(tmp_path: Path) -> None:
    """Two halves of one contract, and neither half is optional.

    A materialized template is shared by every sandbox on the worker, each a
    different unprivileged UID that does not own the cache. Without `x` on the
    directories the mount still succeeds and the first exec fails with EACCES,
    which is the least legible way for an environment to be broken.

    The other half is why the modes are not simply widened: an environment is
    packed from a sandbox, and a sandbox writes what it is asked to write --
    including a key. Granting read to every file would publish whatever a
    template happens to contain, to every sandbox on the worker.

    Both halves were found by hand, in a two-cluster run: a file written through
    the File API is 0600, so the template published from it mounted, attached,
    and then answered `Permission denied` to the sandbox that attached it.
    """

    source = tmp_path / "source"
    (source / "bin").mkdir(parents=True)
    (source / "owner-only").mkdir()
    interpreter = source / "bin" / "python"
    interpreter.write_text("#!/bin/sh\n")
    key = source / "private.key"
    key.write_text("secret\n")
    nested = source / "owner-only" / "notes.txt"
    nested.write_text("notes\n")
    os.chmod(interpreter, 0o755)
    os.chmod(key, 0o600)
    os.chmod(nested, 0o640)
    os.chmod(source / "owner-only", 0o700)

    archive = tmp_path / "env.tar.gz"
    digest, _size = archive_directory(source, archive)
    cache = LocalTemplateCache(tmp_path / "cache")

    tree = cache.materialize(
        "env", digest, lambda destination: destination.write_bytes(archive.read_bytes())
    )

    def mode(relative: str) -> int:
        return stat.S_IMODE((tree / relative).stat().st_mode)

    # Every directory in the tree, including the root the staging rename left 0700.
    for directory in (".", "bin", "owner-only"):
        assert mode(directory) & 0o055 == 0o055, (directory, oct(mode(directory)))

    # And the files kept what their author chose, so a private key stays private
    # and an interpreter stays executable.
    assert mode("private.key") == 0o600
    assert mode("owner-only/notes.txt") == 0o640
    assert mode("bin/python") == 0o755
