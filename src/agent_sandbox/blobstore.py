"""S3-compatible checkpoint store using the standard boto3 credential chain."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, Protocol, cast
from urllib.parse import urlparse

from .config import Settings
from .plugins import OBJECT_STORE_GROUP, create_from_plugin

logger = logging.getLogger(__name__)


class BlobStoreError(RuntimeError):
    pass


class BlobStoreUnavailable(BlobStoreError):
    """The store answered, and could not serve this request.

    Distinct from an object that is not there, and the distinction is load
    bearing: the template catalog treats absence as "nothing published yet", so a
    bucket that does not exist reported as `FileNotFoundError` reads as an empty
    catalog on every worker. A bucket that is missing, credentials that cannot
    write, and an endpoint that cannot be reached are properties of the
    deployment.

    The message is the code the API returns. The part an operator needs — which
    bucket, which endpoint, which S3 error — goes to the log instead, because the
    body of a rejected request is not the place to describe a deployment's
    object store.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.detail = detail


# What botocore raises when the endpoint cannot be reached or the credentials
# cannot be used, by name: botocore is an optional dependency of this module and
# importing it here would make it mandatory. Kept as a closed set so an
# unrecognized failure is re-raised as itself rather than relabelled.
_UNREACHABLE = frozenset(
    {
        "EndpointConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "ConnectionClosedError",
        "NoCredentialsError",
        "CredentialRetrievalError",
        "SSLError",
    }
)


def _error_code(error: Exception) -> tuple[str, int | None]:
    """The `Code` and HTTP status out of a botocore error, when it carries them."""

    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return "", None
    block = response.get("Error", {})
    code = str(block.get("Code", "")) if isinstance(block, dict) else ""
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code, status if isinstance(status, int) else None


def _is_missing_bucket(error: Exception) -> bool:
    code, status = _error_code(error)
    if code == "NoSuchBucket":
        return True
    # S3 implementations differ on what a missing bucket looks like to HEAD: some
    # answer `NoSuchBucket`, others a bare 404 that never names the bucket.
    return status == 404 and "bucket" in str(error).lower()


def _is_store_unavailable(error: Exception) -> bool:
    """Whether the store produced this failure, rather than this service.

    Anything botocore shaped came from the endpoint, including the answers that
    mean "no": a bucket that is not there, credentials that may not write, a
    request the store rejected. A failure with none of those marks — a TypeError
    in this code, a missing file on the way in — is left as itself, so a bug is
    not reported as an unavailable deployment dependency.
    """

    if isinstance(getattr(error, "response", None), dict):
        return True
    return isinstance(error, OSError) or type(error).__name__ in _UNREACHABLE


def _as_not_found(error: Exception, key: str) -> Exception:
    """Translate a backend's "no such key" into the stdlib's spelling of it.

    `FileNotFoundError` is what a caller can portably catch: a plugin store
    raises it naturally, and `download_to` is documented to raise it, so the
    template catalog does not have to know which object store it is talking to.

    A missing *bucket* is deliberately not translated this way. It is the same
    404 from the same call, and telling them apart is the whole reason this
    function exists — see `BlobStoreUnavailable`.
    """

    if _is_missing_bucket(error):
        return error
    code, status = _error_code(error)
    if code in {"NoSuchKey", "404", "NotFound"} or status == 404:
        return FileNotFoundError(f"{key} is not in the object store")
    return error



def _copy_chunks(body: Any, target: BinaryIO, chunk_size: int) -> None:
    while chunk := body.read(chunk_size):
        target.write(chunk)


def _plain_env_credentials() -> tuple[str | None, str | None]:
    access_key = os.getenv("BLOBSTORE_ACCESS_KEY") or None
    secret_key = os.getenv("BLOBSTORE_SECRET_KEY") or None
    if bool(access_key) != bool(secret_key):
        raise BlobStoreError(
            "BlobStore access key and secret key must be set together or not at all"
        )
    return access_key, secret_key


def _boto_config(region: str) -> Any:
    try:
        from botocore.config import Config
    except ImportError as exc:
        raise BlobStoreError("S3 backend requires the 's3' optional dependency") from exc
    return Config(
        region_name=region,
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": 5, "mode": "standard"},
    )


def create_s3_client(endpoint: str, region: str) -> Any:
    """Create a path-style SigV4 client with optional explicit credentials."""
    config = _boto_config(region)
    import boto3

    access_key, secret_key = _plain_env_credentials()
    kwargs: dict[str, Any] = {}
    if access_key and secret_key:
        kwargs.update(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=region,
        config=config,
        **kwargs,
    )


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def get(self, key_or_uri: str) -> bytes: ...
    def delete(self, key_or_uri: str) -> None: ...


class BlobStore:
    """Namespaced checkpoint objects addressed by relative keys or blob URIs."""

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        if not settings.blobstore_bucket:
            raise BlobStoreError("BLOBSTORE_BUCKET is not configured")
        self.settings = settings
        self._client: Any = client
        self._base_prefix = _normalize_prefix(settings.blobstore_base_prefix)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.blobstore_bucket)

    @property
    def base_prefix(self) -> str:
        return self._base_prefix

    def _s3(self) -> Any:
        if self._client is None:
            self._client = create_s3_client(
                self.settings.blobstore_endpoint, self.settings.blobstore_region
            )
        return self._client

    def _unavailable(self, operation: str, error: Exception) -> BlobStoreUnavailable:
        """Log what an operator needs and return the code a caller gets."""

        bucket = self.settings.blobstore_bucket
        endpoint = self.settings.blobstore_endpoint or "the default endpoint"
        code, _ = _error_code(error)
        logger.error(
            "object store could not %s bucket %r at %s (%s): %s -- create the "
            "bucket, or point BLOBSTORE_BUCKET at one that exists",
            operation,
            bucket,
            endpoint,
            code or type(error).__name__,
            error,
        )
        return BlobStoreUnavailable("OBJECT_STORE_UNAVAILABLE", f"{bucket}@{endpoint}")

    def _store_error(self, operation: str, error: Exception, key: str | None = None) -> Exception:
        """The exception to raise for a failed store call.

        Ordered so absence keeps its own error: an object that is not there is
        `FileNotFoundError` whatever else is true of it, and only then is
        everything the store itself said reported as the deployment problem it is.
        """

        if key is not None:
            absent = _as_not_found(error, key)
            if isinstance(absent, FileNotFoundError):
                return absent
        if _is_store_unavailable(error):
            return self._unavailable(operation, error)
        return error

    # ── deployment ──
    def ensure_bucket(self) -> None:
        """Create the configured bucket if the endpoint does not have it yet.

        Naming an endpoint and a bucket is what makes templates cross workers,
        and a new S3 account, a MinIO install and a LocalStack container all
        begin without it. The first publish is where that shows up — as
        `NoSuchBucket` from a boto3 call stack, which reads as a bug in this
        service rather than as one missing prerequisite — so the bucket is created
        here instead.

        Never fatal. A store that cannot be reached, or credentials that may read
        but not create buckets, are logged and left for the operation that needs
        them: refusing to start over a bucket the deployment may not be using yet
        is a worse failure than the one it prevents.
        """

        bucket = self.settings.blobstore_bucket
        client = self._s3()
        try:
            client.head_bucket(Bucket=bucket)
            return
        except Exception as error:
            # A bucket that is not there, whichever way this endpoint spells it.
            if not _is_missing_bucket(error) and _error_code(error)[1] != 404:
                logger.warning(
                    "object store %s could not be asked about bucket %r (%s); "
                    "templates will not cross workers until it answers",
                    self.settings.blobstore_endpoint or "at the default endpoint",
                    bucket,
                    error,
                )
                return

        parameters: dict[str, Any] = {"Bucket": bucket}
        region = self.settings.blobstore_region
        # Every region but the original one requires a location constraint, and
        # S3 rejects the request when it names `us-east-1` explicitly.
        if region and region != "us-east-1":
            parameters["CreateBucketConfiguration"] = {"LocationConstraint": region}
        try:
            client.create_bucket(**parameters)
        except Exception as error:
            logger.warning(
                "object store bucket %r does not exist and could not be created "
                "(%s); create it, or set BLOBSTORE_BUCKET to one that exists",
                bucket,
                error,
            )
            return
        logger.info(
            "created object store bucket %r at %s",
            bucket,
            self.settings.blobstore_endpoint or "the default endpoint",
        )

    # ── key/uri helpers ──
    def full_key(self, key_or_uri: str) -> str:
        relative = self.key_from_uri(key_or_uri) if key_or_uri.startswith("blob://") else key_or_uri
        relative = relative.lstrip("/")
        if not relative:
            raise BlobStoreError("BlobStore key must not be empty")
        if self._base_prefix and relative.startswith(self._base_prefix):
            return relative
        return f"{self._base_prefix}{relative}"

    @staticmethod
    def uri(key: str) -> str:
        normalized = key.lstrip("/")
        if not normalized:
            raise BlobStoreError("BlobStore key must not be empty")
        return f"blob://{normalized}"

    @staticmethod
    def key_from_uri(uri: str) -> str:
        parsed = urlparse(uri)
        if parsed.scheme != "blob":
            raise BlobStoreError(f"not a blob:// URI: {uri!r}")
        key = f"{parsed.netloc}{parsed.path}".lstrip("/")
        if not key:
            raise BlobStoreError("blob:// URI is missing an object key")
        return key

    # ── data ops ──
    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        kwargs: dict[str, Any] = {
            "Bucket": self.settings.blobstore_bucket,
            "Key": self.full_key(key),
            "Body": data,
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            self._s3().put_object(**kwargs)
        except Exception as error:
            raise self._store_error("write", error) from error
        return self.uri(key)

    def get(self, key_or_uri: str) -> bytes:
        try:
            response = self._s3().get_object(
                Bucket=self.settings.blobstore_bucket, Key=self.full_key(key_or_uri)
            )
        except Exception as error:
            raise self._store_error("read", error, key_or_uri) from error
        return bytes(response["Body"].read())

    def upload_file(self, key: str, path: str | Path) -> str:
        with Path(path).open("rb") as stream:
            try:
                self._s3().upload_fileobj(
                    stream, self.settings.blobstore_bucket, self.full_key(key)
                )
            except Exception as error:
                raise self._store_error("write", error) from error
        return self.uri(key)

    def download_to(
        self,
        key_or_uri: str,
        destination: str | Path | BinaryIO,
        *,
        chunk_size: int = 8 * 1024 * 1024,
    ) -> None:
        """Write an object to a path, or into an open binary stream.

        Both, because two callers want different things and neither is wrong.
        The template protocol hands a destination path — the store owns opening
        and closing it, and a plugin store should not have to care about streams
        — while checkpoint restore already holds an open stream and passes it.

        A missing object raises `FileNotFoundError` rather than a client error,
        so a caller can tell "not there yet" from "the store is broken". The
        template catalog depends on that distinction: before the first publish
        the catalog is legitimately absent, and treating every other failure the
        same way once hid a store that could not download at all.
        """

        try:
            response = self._s3().get_object(
                Bucket=self.settings.blobstore_bucket, Key=self.full_key(key_or_uri)
            )
        except Exception as error:
            raise self._store_error("read", error, key_or_uri) from error

        body = response["Body"]
        if isinstance(destination, (str, Path)):
            with Path(destination).open("wb") as stream:
                _copy_chunks(body, stream, chunk_size)
            return
        _copy_chunks(body, destination, chunk_size)

    def list(self, prefix: str) -> Iterator[tuple[str, int, Any]]:
        paginator = self._s3().get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self.settings.blobstore_bucket, Prefix=self.full_key(prefix)
            ):
                for item in page.get("Contents", []):
                    physical_key = str(item["Key"])
                    relative = physical_key.removeprefix(self._base_prefix)
                    yield relative, int(item["Size"]), item.get("LastModified")
        except Exception as error:
            raise self._store_error("list", error) from error

    def presign_get(self, key_or_uri: str, *, expires_seconds: int = 3600) -> str:
        _validate_expires(expires_seconds)
        return str(
            self._s3().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.settings.blobstore_bucket,
                    "Key": self.full_key(key_or_uri),
                },
                ExpiresIn=expires_seconds,
            )
        )

    def presign_put(self, key_or_uri: str, *, expires_seconds: int = 3600) -> str:
        """Issue a short-lived URL so a sandbox uploads directly, bypassing this service."""
        _validate_expires(expires_seconds)
        return str(
            self._s3().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.settings.blobstore_bucket,
                    "Key": self.full_key(key_or_uri),
                },
                ExpiresIn=expires_seconds,
            )
        )

    def delete(self, key_or_uri: str) -> None:
        try:
            self._s3().delete_object(
                Bucket=self.settings.blobstore_bucket, Key=self.full_key(key_or_uri)
            )
        except Exception as error:
            raise self._store_error("delete", error) from error

    # ── sandbox checkpoint helpers ──
    def upload_checkpoint(self, sandbox_id: str, archive: Path) -> str:
        return self.upload_file(f"checkpoints/{sandbox_id}/{archive.name}", archive)


def _normalize_prefix(prefix: str) -> str:
    value = prefix.strip().strip("/")
    return f"{value}/" if value else ""


def _validate_expires(expires_seconds: int) -> None:
    if not 1 <= expires_seconds <= 86400:
        raise BlobStoreError("presigned URL lifetime must be between 1 second and 24 hours")


def create_object_store(settings: Settings) -> ObjectStore | None:
    """Return the configured checkpoint store; S3 is optional and standards-based."""
    if settings.object_store_backend == "disabled":
        return None
    if settings.object_store_backend == "s3":
        if not settings.blobstore_bucket:
            return None
        return BlobStore(settings)
    return cast(
        "ObjectStore",
        create_from_plugin(OBJECT_STORE_GROUP, settings.object_store_backend, settings),
    )


__all__ = [
    "BlobStore",
    "BlobStoreError",
    "BlobStoreUnavailable",
    "ObjectStore",
    "create_object_store",
    "create_s3_client",
]
