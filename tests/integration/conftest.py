"""Shared fixtures for integration tests that require real middleware.

Every test in this package talks to an actual server started by
compose.integration.yaml. Tests skip rather than fail when a backend is absent,
so `uv run pytest` on a developer machine without Docker stays green.
"""

from __future__ import annotations

import os
import socket
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

MYSQL_URL = os.getenv(
    "SANDBOX_TEST_MYSQL_URL",
    "mysql+aiomysql://root:integration@127.0.0.1:13306/agent_sandbox",
)
POSTGRES_URL = os.getenv(
    "SANDBOX_TEST_POSTGRES_URL",
    "postgresql+asyncpg://postgres:integration@127.0.0.1:15432/agent_sandbox",
)
REDIS_URL = os.getenv("SANDBOX_TEST_REDIS_URL", "redis://127.0.0.1:16379/0")
MINIO_ENDPOINT = os.getenv("SANDBOX_TEST_MINIO_ENDPOINT", "http://127.0.0.1:19000")
MINIO_ACCESS_KEY = os.getenv("SANDBOX_TEST_MINIO_ACCESS_KEY", "integration")
MINIO_SECRET_KEY = os.getenv("SANDBOX_TEST_MINIO_SECRET_KEY", "integration-secret")


def _port_open(url: str) -> bool:
    """Check reachability before importing a driver, so skips stay fast."""
    from sqlalchemy.engine import make_url

    parsed = make_url(url)
    host, port = parsed.host, parsed.port
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return True
    except OSError:
        return False


def _http_port_open(endpoint: str) -> bool:
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    if not parsed.hostname or not parsed.port:
        return False
    try:
        with socket.create_connection((parsed.hostname, parsed.port), timeout=1.5):
            return True
    except OSError:
        return False


def require_backend(url: str, driver: str) -> None:
    """Skip unless both the driver and a live server are available."""
    pytest.importorskip(driver)
    if not _port_open(url):
        pytest.skip(f"no server reachable at {url}; start compose.integration.yaml")


@pytest.fixture(scope="session")
def mysql_url() -> str:
    require_backend(MYSQL_URL, "aiomysql")
    return MYSQL_URL


@pytest.fixture(scope="session")
def postgres_url() -> str:
    require_backend(POSTGRES_URL, "asyncpg")
    return POSTGRES_URL


@pytest.fixture(scope="session")
def redis_url() -> str:
    pytest.importorskip("redis")
    if not _port_open(REDIS_URL):
        pytest.skip(f"no Redis reachable at {REDIS_URL}; start compose.integration.yaml")
    return REDIS_URL


@pytest.fixture(scope="session")
def minio_endpoint() -> str:
    pytest.importorskip("boto3")
    if not _http_port_open(MINIO_ENDPOINT):
        pytest.skip(f"no MinIO reachable at {MINIO_ENDPOINT}; start compose.integration.yaml")
    return MINIO_ENDPOINT


@pytest.fixture(scope="session")
def s3_credentials() -> tuple[str, str]:
    """Access and secret key for the S3-compatible store (LocalStack, RustFS, MinIO, ...)."""
    return MINIO_ACCESS_KEY, MINIO_SECRET_KEY


@pytest.fixture
def settings_factory(tmp_path: Path) -> Any:
    """Build Settings pointed at a real database with a per-test workspace."""
    from agent_sandbox.config import Settings

    def build(database_url: str, **overrides: Any) -> Settings:
        options: dict[str, Any] = {
            "internal_token": "integration-token",
            "local_root": tmp_path / "sandboxes",
            "database_url": database_url,
            "database_auto_ddl": True,
            "profile_hash": "profile-integration",
        }
        options.update(overrides)
        return Settings(**options)

    return build


@pytest.fixture
async def clean_database(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    """Yield a connected store and drop every table afterwards.

    Integration databases are shared across the session, so each test must leave
    no rows behind or lock-contention assertions become order dependent.
    """
    from agent_sandbox.sql_database import SqlAlchemyDatabase, metadata

    settings = request.param
    database = SqlAlchemyDatabase(settings)
    await database.connect()
    engine = database.engine
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(metadata.drop_all)
        await connection.run_sync(metadata.create_all)
    try:
        yield database
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.drop_all)
        await database.close()


@pytest.fixture
def no_chown(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Workspace setup calls os.chown, which needs root on Linux and fails on macOS."""
    if os.geteuid() != 0:
        monkeypatch.setattr(os, "chown", lambda *_args, **_kwargs: None)
    yield
