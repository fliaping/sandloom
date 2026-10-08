"""BlobStore credential resolution tests."""

from __future__ import annotations

import io
import sys
from types import ModuleType
from typing import Any

import pytest

from agent_sandbox.app import status_for_error
from agent_sandbox.blobstore import (
    BlobStore,
    BlobStoreError,
    BlobStoreUnavailable,
    _plain_env_credentials,
    create_s3_client,
)
from agent_sandbox.config import Settings


def test_plain_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOBSTORE_ACCESS_KEY", "plain-ak")
    monkeypatch.setenv("BLOBSTORE_SECRET_KEY", "plain-sk")

    assert _plain_env_credentials() == ("plain-ak", "plain-sk")


def test_plain_environment_requires_complete_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLOBSTORE_ACCESS_KEY", "plain-ak")
    monkeypatch.delenv("BLOBSTORE_SECRET_KEY", raising=False)

    with pytest.raises(BlobStoreError, match="must be set together"):
        _plain_env_credentials()


def test_s3_uses_explicit_environment_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    boto3 = ModuleType("boto3")

    def fake_client(*args: Any, **kwargs: Any) -> object:
        calls.append((args, kwargs))
        return object()

    boto3.client = fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setenv("BLOBSTORE_ACCESS_KEY", "plain-ak")
    monkeypatch.setenv("BLOBSTORE_SECRET_KEY", "plain-sk")

    assert create_s3_client("https://s3.example", "us-east-1") is not None
    assert calls[0][0] == ("s3",)
    assert calls[0][1]["aws_access_key_id"] == "plain-ak"
    assert calls[0][1]["aws_secret_access_key"] == "plain-sk"


def test_s3_uses_standard_boto_credential_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    boto3 = ModuleType("boto3")

    def fake_client(_: str, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    boto3.client = fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.delenv("BLOBSTORE_ACCESS_KEY", raising=False)
    monkeypatch.delenv("BLOBSTORE_SECRET_KEY", raising=False)

    assert create_s3_client("", "us-east-1") is not None
    assert calls[0]["endpoint_url"] is None
    assert "aws_access_key_id" not in calls[0]
    assert "aws_secret_access_key" not in calls[0]


# ── the object store a deployment names, and what it does when it is not there ──


class ClientError(Exception):
    """The shape botocore raises, without importing botocore.

    The `s3` extra is not installed in every environment that runs this suite —
    the CI test job syncs without extras — so the tests fabricate the one
    attribute the translation reads rather than skipping when it is absent.
    """

    def __init__(self, code: str, status: int, message: str = "") -> None:
        super().__init__(f"An error occurred ({code}) when calling S3: {message}")
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3:
    def __init__(self, **failures: Exception) -> None:
        self.failures = failures
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _do(self, name: str, **kwargs: Any) -> None:
        self.calls.append((name, kwargs))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._do("head_bucket", **kwargs)
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._do("create_bucket", **kwargs)
        return {}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._do("put_object", **kwargs)
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._do("get_object", **kwargs)
        return {"Body": io.BytesIO(b"checkpoint")}

    def upload_fileobj(self, _stream: Any, bucket: str, key: str) -> None:
        self._do("upload_fileobj", Bucket=bucket, Key=key)

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._do("delete_object", **kwargs)
        return {}


def _store(client: FakeS3, **overrides: Any) -> BlobStore:
    settings: dict[str, Any] = {
        "internal_token": "token",
        "blobstore_endpoint": "http://object-store:9000",
        "blobstore_bucket": "agent-sandbox-templates",
        "blobstore_region": "us-east-1",
        "blobstore_base_prefix": "agent-sandbox/",
    }
    return BlobStore(Settings(**{**settings, **overrides}), client=client)


def _names(client: FakeS3) -> list[str]:
    return [name for name, _ in client.calls]


def test_a_missing_bucket_is_created_before_the_first_publish() -> None:
    """The documented multi-replica configuration names a bucket nobody created.

    A fresh S3 account, a MinIO install and a LocalStack container all begin with
    none, and the first template publish is where that surfaces — as
    `NoSuchBucket` from a boto3 call stack, which reads as a bug in this service.
    """

    client = FakeS3(head_bucket=ClientError("NoSuchBucket", 404, "The specified bucket does not exist"))

    _store(client).ensure_bucket()

    assert _names(client) == ["head_bucket", "create_bucket"]
    assert client.calls[1][1] == {"Bucket": "agent-sandbox-templates"}


def test_a_bucket_that_exists_is_not_touched() -> None:
    """Creating it again would fail, and creating it is not why we are here."""

    client = FakeS3()

    _store(client).ensure_bucket()

    assert _names(client) == ["head_bucket"]


def test_a_vague_404_is_read_as_a_missing_bucket_too() -> None:
    """Some implementations answer HEAD with a bare 404 that never says `NoSuchBucket`."""

    client = FakeS3(head_bucket=ClientError("404", 404, "the bucket does not exist"))

    _store(client).ensure_bucket()

    assert _names(client) == ["head_bucket", "create_bucket"]


def test_a_store_with_no_route_does_not_stop_the_worker() -> None:
    """Refusing to start over a bucket this deployment may not use yet is worse."""

    client = FakeS3(head_bucket=ConnectionError("no route to host"))

    _store(client).ensure_bucket()

    assert _names(client) == ["head_bucket"]


def test_a_bucket_that_cannot_be_created_is_an_error_message_not_a_crash(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Not permitted to create buckets is a normal policy, not a startup failure."""

    client = FakeS3(
        head_bucket=ClientError("NoSuchBucket", 404, "no such bucket"),
        create_bucket=ClientError("AccessDenied", 403, "not permitted"),
    )

    with caplog.at_level("WARNING", logger="agent_sandbox.blobstore"):
        _store(client).ensure_bucket()

    assert "BLOBSTORE_BUCKET" in caplog.text
    assert "agent-sandbox-templates" in caplog.text


def test_another_region_gets_a_location_constraint() -> None:
    """S3 rejects a create in any other region without one, and rejects the
    constraint when the region is the original `us-east-1`."""

    client = FakeS3(head_bucket=ClientError("NoSuchBucket", 404, "no such bucket"))

    _store(client, blobstore_region="eu-central-1").ensure_bucket()

    assert client.calls[1][1]["CreateBucketConfiguration"] == {
        "LocationConstraint": "eu-central-1"
    }


def test_a_write_to_a_bucket_that_is_not_there_is_reported_as_the_store() -> None:
    """Not as a 500 with a boto3 call stack, and not as a property of the request."""

    client = FakeS3(put_object=ClientError("NoSuchBucket", 404, "no such bucket"))

    with pytest.raises(BlobStoreUnavailable) as raised:
        _store(client).put("checkpoints/a", b"data")

    assert str(raised.value) == "OBJECT_STORE_UNAVAILABLE"
    assert raised.value.detail == "agent-sandbox-templates@http://object-store:9000"
    assert status_for_error(raised.value) == 503


def test_an_object_that_is_absent_is_not_a_store_that_is_broken(tmp_path: Any) -> None:
    """The distinction the catalog turns on: nothing published yet, versus a
    store no worker can read from. Both are a 404 from the same call."""

    key = ClientError("NoSuchKey", 404, "the key does not exist")
    absent = FakeS3(get_object=key)
    with pytest.raises(FileNotFoundError):
        _store(absent).download_to("templates/a", tmp_path / "out")

    bucket = ClientError("NoSuchBucket", 404, "no such bucket")
    missing = FakeS3(get_object=bucket)
    with pytest.raises(BlobStoreUnavailable):
        _store(missing).download_to("templates/a", tmp_path / "out")


def test_a_failure_that_did_not_come_from_the_store_is_left_as_itself() -> None:
    """A bug here must not be reported as an unavailable deployment dependency."""

    client = FakeS3(put_object=TypeError("wrong number of arguments"))

    with pytest.raises(TypeError):
        _store(client).put("checkpoints/a", b"data")
