"""Object store behavior against a real S3 API implementation.

The unit suite fakes `boto3` with a synthetic module, so it never verifies that
the requests this code builds are actually accepted by an S3 server. These tests
run against LocalStack, including presigned URLs, which are the easiest thing to
get subtly wrong.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from agent_sandbox.blobstore import BlobStore, BlobStoreUnavailable
from agent_sandbox.config import Settings

BUCKET = "agent-sandbox-integration"


@pytest.fixture
def s3_client(minio_endpoint: str, s3_credentials: tuple[str, str]) -> Any:
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=s3_credentials[0],
        aws_secret_access_key=s3_credentials[1],
        region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


@pytest.fixture
def store(minio_endpoint: str, s3_client: Any) -> Iterator[BlobStore]:
    """A BlobStore on a per-test prefix so listings cannot see other tests."""
    try:
        s3_client.create_bucket(Bucket=BUCKET)
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        pass
    except s3_client.exceptions.BucketAlreadyExists:
        pass

    prefix = f"test-{time.time_ns()}/"
    settings = Settings(
        internal_token="integration-token",
        blobstore_endpoint=minio_endpoint,
        blobstore_bucket=BUCKET,
        blobstore_base_prefix=prefix,
        blobstore_region="us-east-1",
    )
    instance = BlobStore(settings, client=s3_client)
    try:
        yield instance
    finally:
        listing = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
        contents = listing.get("Contents", [])
        if contents:
            s3_client.delete_objects(
                Bucket=BUCKET,
                Delete={"Objects": [{"Key": item["Key"]} for item in contents]},
            )


def test_put_and_get_round_trip(store: BlobStore) -> None:
    uri = store.put("checkpoints/sandbox-a/state.tar", b"payload-bytes")

    assert uri == "blob://checkpoints/sandbox-a/state.tar"
    assert store.get(uri) == b"payload-bytes"
    # A relative key must address the same object as the blob:// URI.
    assert store.get("checkpoints/sandbox-a/state.tar") == b"payload-bytes"


def test_binary_payloads_are_not_corrupted(store: BlobStore) -> None:
    payload = bytes(range(256)) * 512

    store.put("binary/all-bytes.bin", payload)

    assert store.get("binary/all-bytes.bin") == payload


def test_base_prefix_is_applied_once(store: BlobStore, s3_client: Any) -> None:
    """`full_key()` must be idempotent, or retries double the prefix."""
    store.put("checkpoints/state.tar", b"data")
    physical = f"{store.base_prefix}checkpoints/state.tar"

    listing = s3_client.list_objects_v2(Bucket=BUCKET, Prefix=store.base_prefix)
    keys = [item["Key"] for item in listing.get("Contents", [])]

    assert physical in keys
    assert store.full_key(physical) == physical


def test_content_type_is_preserved(store: BlobStore, s3_client: Any) -> None:
    store.put("meta/report.json", b"{}", content_type="application/json")

    head = s3_client.head_object(Bucket=BUCKET, Key=f"{store.base_prefix}meta/report.json")

    assert head["ContentType"] == "application/json"


def test_upload_file_streams_from_disk(store: BlobStore, tmp_path: Path) -> None:
    archive = tmp_path / "state.tar"
    payload = b"archive-content" * 1024
    archive.write_bytes(payload)

    uri = store.upload_file("checkpoints/sandbox-a/state.tar", archive)

    assert store.get(uri) == payload


def test_download_to_writes_every_chunk(store: BlobStore, tmp_path: Path) -> None:
    payload = b"x" * (3 * 1024 * 1024)
    store.put("large/blob.bin", payload)
    target = tmp_path / "downloaded.bin"

    with target.open("wb") as stream:
        store.download_to("large/blob.bin", stream, chunk_size=64 * 1024)

    assert target.read_bytes() == payload


def test_download_to_writes_to_a_path(store: BlobStore, tmp_path: Path) -> None:
    """The form the template protocol requires.

    `TemplateObjectStore.download_to` hands a destination path — the store owns
    opening and closing it — while checkpoint restore hands an open stream.
    The stream form was the only one tested, so the real store accepted a
    `Path` and then failed on it with an `AttributeError` from inside boto3's
    response body, on every fetch, in production.
    """

    payload = b"environment" * 1024
    store.put("templates/env/archive.tar.gz", payload)
    destination = tmp_path / "nested" / "archive.tar.gz"
    destination.parent.mkdir()

    store.download_to("templates/env/archive.tar.gz", destination)

    assert destination.read_bytes() == payload


def test_download_to_reports_a_missing_object_as_not_found(store: BlobStore, tmp_path: Path) -> None:
    """The catalog depends on telling "absent" apart from "broken"."""

    with pytest.raises(FileNotFoundError):
        store.download_to("templates/never-built/archive.tar.gz", tmp_path / "x")


def test_a_template_survives_the_round_trip_through_s3(store: BlobStore, tmp_path: Path) -> None:
    """Build on one "worker", fetch on another, through the real store."""

    from agent_sandbox.templates import LocalTemplateCache, TemplateManager

    tree = tmp_path / "env" / "bin"
    tree.mkdir(parents=True)
    (tree / "activate").write_text("#!/bin/sh\n")
    (tree / "activate").chmod(0o755)
    (tmp_path / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")

    builder = TemplateManager(LocalTemplateCache(tmp_path / "worker-a"), object_store=store)
    record = builder.build(name="env", source=tmp_path / "env")

    consumer = TemplateManager(LocalTemplateCache(tmp_path / "worker-b"), object_store=store)
    materialized = consumer.materialize(record)

    assert (materialized / "pyvenv.cfg").read_text() == "home = /usr/bin\n"
    # An environment that lost its executable bits is not an environment.
    assert (materialized / "bin" / "activate").stat().st_mode & 0o111


def test_an_evicted_revision_is_fetched_again_from_the_store(
    store: BlobStore, tmp_path: Path
) -> None:
    """Eviction has to be recoverable, or a cache budget breaks sandboxes.

    The cache is a copy and the store is what it is a copy of, so a revision
    that pruning removed is downloaded and extracted again on the next attach
    rather than reported as a template that no longer exists.
    """

    from agent_sandbox.templates import LocalTemplateCache, TemplateManager

    tree = tmp_path / "env" / "bin"
    tree.mkdir(parents=True)
    (tree / "pyvenv.cfg").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (tree / "activate").write_text("#!/bin/sh\n")

    cache = LocalTemplateCache(tmp_path / "worker", max_total_bytes=1)
    worker = TemplateManager(cache, object_store=store)
    record = worker.build(name="env", source=tmp_path / "env")
    assert cache.has("env", record.digest)

    # A budget of one byte and nothing pinned: the sweep takes everything.
    assert cache.prune() == [("env", record.digest)]
    assert not cache.has("env", record.digest)

    materialized = worker.materialize(record)

    assert cache.has("env", record.digest)
    assert (materialized / "pyvenv.cfg").read_text() == "home = /usr/bin\n"
    assert (materialized / "bin" / "activate").read_text() == "#!/bin/sh\n"


def test_a_tampered_object_is_refused_by_the_consumer(store: BlobStore, tmp_path: Path) -> None:
    """The digest is rechecked at materialization, not trusted from the catalog.

    A template is one read-only tree shared by every tenant on a worker, so
    mounting bytes that are not the ones the catalog published would hand every
    sandbox that asks for the name an environment nobody built. Overwriting the
    object is the cheapest way to get there: the name and the key still look
    right, and only the content is wrong.
    """

    from agent_sandbox.templates import (
        LocalTemplateCache,
        TemplateError,
        TemplateManager,
        archive_directory,
    )

    tree = tmp_path / "env" / "bin"
    tree.mkdir(parents=True)
    (tree / "activate").write_text("#!/bin/sh\n")
    (tree / "activate").chmod(0o755)
    (tmp_path / "env" / "pyvenv.cfg").write_text("home = /usr/bin\n")

    builder = TemplateManager(LocalTemplateCache(tmp_path / "worker-a"), object_store=store)
    record = builder.build(name="env", source=tmp_path / "env")

    # Same key, and a *valid* archive of a different tree, so the only thing
    # wrong with it is that it is not the archive the digest names. Garbage bytes
    # would be refused by the extractor and prove nothing about the digest.
    other = tmp_path / "other" / "bin"
    other.mkdir(parents=True)
    (other / "activate").write_text("#!/bin/sh\necho not yours\n")
    impostor = tmp_path / "impostor.tar.gz"
    archive_directory(tmp_path / "other", impostor)
    store.upload_file(record.object_key, impostor)

    consumer = TemplateManager(LocalTemplateCache(tmp_path / "worker-b"), object_store=store)
    with pytest.raises(TemplateError, match="digest mismatch"):
        consumer.materialize(record)

    # A refused fetch must not leave an extracted tree for a later mount to find,
    # and the failed archive must not be left occupying the staging directory.
    assert not consumer.cache.has(record.name, record.digest)
    staging = consumer.cache.staging_root
    assert not (staging.exists() and any(staging.iterdir()))


def test_list_returns_relative_keys(store: BlobStore) -> None:
    store.put("checkpoints/a.tar", b"a")
    store.put("checkpoints/b.tar", b"bb")
    store.put("other/c.tar", b"ccc")

    items = sorted(store.list("checkpoints/"))

    assert [key for key, _size, _modified in items] == [
        "checkpoints/a.tar",
        "checkpoints/b.tar",
    ]
    assert [size for _key, size, _modified in items] == [1, 2]


def test_delete_removes_the_object(store: BlobStore) -> None:
    """And reading it back is an absent object, not a broken store.

    The assertion used to accept any exception whose message matched
    `NoSuchKey|404`, which is the backend's own error arriving through this
    class: it passes whether or not a caller can tell "not there yet" from "no
    worker can read the store", and the template catalog is built on being able
    to. `get` now raises what `download_to` has always raised.
    """

    store.put("checkpoints/state.tar", b"data")

    store.delete("checkpoints/state.tar")

    with pytest.raises(FileNotFoundError):
        store.get("checkpoints/state.tar")


def test_presigned_get_is_usable_without_credentials(store: BlobStore) -> None:
    """A URL the sandbox can fetch directly; if signing is wrong this 403s."""
    store.put("checkpoints/state.tar", b"presigned-payload")

    url = store.presign_get("checkpoints/state.tar", expires_seconds=300)
    response = httpx.get(url, timeout=10.0)

    assert response.status_code == 200
    assert response.content == b"presigned-payload"


def test_presigned_put_accepts_a_direct_upload(store: BlobStore) -> None:
    url = store.presign_put("uploads/direct.bin", expires_seconds=300)

    response = httpx.put(url, content=b"direct-upload", timeout=10.0)

    assert response.status_code in {200, 204}
    assert store.get("uploads/direct.bin") == b"direct-upload"


def test_upload_checkpoint_uses_the_conventional_key(store: BlobStore, tmp_path: Path) -> None:
    archive = tmp_path / "gen-3.tar"
    archive.write_bytes(b"checkpoint")

    uri = store.upload_checkpoint("sandbox-a", archive)

    assert uri == "blob://checkpoints/sandbox-a/gen-3.tar"
    assert store.get(uri) == b"checkpoint"

    # The other half of the story the docs tell: a caller keeps an archive here
    # and gets it back onto disk to restore it. The URI the upload returned is
    # what addresses it, so this is the path a restore actually takes.
    restored = tmp_path / "restored.tar"
    store.download_to(uri, restored)
    assert restored.read_bytes() == b"checkpoint"


def test_a_missing_bucket_is_not_a_missing_object(
    s3_client: Any, minio_endpoint: str
) -> None:
    """The state a new deployment's first publish happens in.

    Named against a real S3 API because this is where the documented
    multi-replica configuration failed: the bucket did not exist, the archive
    upload answered `NoSuchBucket`, and the caller received a 500 describing
    boto3 internals — while the catalog, which treats a missing object as "not
    published yet", would have reported the store's absence as an empty catalog.
    """

    bucket = f"agent-sandbox-{uuid4().hex[:8]}"
    store = BlobStore(
        Settings(
            internal_token="token",
            blobstore_endpoint=minio_endpoint,
            blobstore_bucket=bucket,
            blobstore_region="us-east-1",
            blobstore_base_prefix="agent-sandbox/",
        ),
        client=s3_client,
    )

    with pytest.raises(BlobStoreUnavailable) as raised:
        store.put("templates/env/archive.tar.gz", b"archive")
    assert str(raised.value) == "OBJECT_STORE_UNAVAILABLE"

    with pytest.raises(BlobStoreUnavailable):
        store.get("templates/env/archive.tar.gz")

    # And the remedy the service applies at startup is enough to make the bucket
    # usable, which is what the fleet verification then does end to end.
    store.ensure_bucket()

    store.put("templates/env/archive.tar.gz", b"archive")
    assert store.get("templates/env/archive.tar.gz") == b"archive"
