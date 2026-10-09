"""Portable service configuration.

Enterprise adapters own their settings and legacy environment translation in
their private distribution. The public core only selects named backends.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from agent_sandbox_runtime import SUPPORTED_TOOLCHAINS, IsolationLevel
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _auto_port0() -> int:
    raw = os.getenv("AUTO_PORT0", "")
    return int(raw) if raw else 8080


# A list that an operator writes as `a,b,c` rather than as a JSON array.
#
# pydantic-settings decodes a complex field from the environment with
# `json.loads` before any validator runs, so the comma form failed with
# `SettingsError: error parsing value for field ...` and the service refused to
# start. Every example an operator would naturally write — a list of
# toolchains, proxies, or mounts — was unusable. `NoDecode` hands the raw
# string to `split_string_lists` below instead.
CommaSeparatedList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    host: str = "0.0.0.0"
    port: int = Field(default_factory=_auto_port0)
    advertise_host: str = Field(default_factory=socket.getfqdn)
    advertise_scheme: str = "http"
    internal_token: str = ""
    profile_id: str = "coding-default"
    profile_hash: str = "bubblewrap-0.11.2-generic-v12"
    isolation_level: str = "auto"
    isolation_required_features: CommaSeparatedList = Field(default_factory=list)
    isolation_optional_features: CommaSeparatedList = Field(default_factory=list)
    network_mode: str = "host"
    execution_backend: str = "bubblewrap"
    worker_capacity: int = 32
    heartbeat_interval_seconds: float = 10.0
    heartbeat_ttl_seconds: int = 30
    startup_log_path: Path = Path("/tmp/agent-sandbox-startup.log")

    database_url: str = ""
    database_auto_ddl: bool | None = None
    metadata_backend: str = "auto"
    mysql_connect_timeout_seconds: int = 10
    mysql_pool_recycle_seconds: int = 300
    registry_backend: str = "memory"
    registry_namespace: str = "agent-sandbox"
    redis_url: str = "redis://127.0.0.1:6379/0"

    local_root: Path = Path("/var/lib/agent-sandbox/sandboxes")
    shared_root: Path | None = None
    uid_start: int = 20000
    uid_end: int = 59999
    idle_ttl_seconds: int = 1800
    orphan_release_grace_seconds: int = 300
    orphan_running_grace_seconds: int = 60
    orphan_reaper_batch_size: int = 100
    maintenance_interval_seconds: float = 60.0
    # How long a suspended (dormant) sandbox keeps its workspace before the
    # maintenance reaper releases it and reclaims the disk. 0 keeps it until a
    # client releases it.
    suspended_retention_seconds: int = Field(default=7 * 24 * 3600, ge=0)
    # Whether a suspend also archives the workspace to the object store, so a
    # resume can land on another worker: `auto` does so for local storage when a
    # store is configured, `always` refuses to suspend without one, `never` keeps
    # the dormant workspace on its worker only.
    suspend_snapshot: str = "auto"
    # How long a workspace directory this worker no longer owns (its sandbox
    # resumed on another worker, or was released while this worker was away)
    # stays on disk before the maintenance cycle deletes it. Counted from when
    # the worker first saw it orphaned, by a marker on disk, so a restart does
    # not reset it. 0 never deletes such directories.
    orphan_dormant_dir_ttl_seconds: int = Field(default=24 * 3600, ge=0)
    disk_high_watermark_percent: int = 90
    min_free_bytes: int = 1024 * 1024 * 1024

    bubblewrap_path: Path = Path("/usr/bin/bwrap")
    setpriv_path: Path = Path("/usr/bin/setpriv")
    prlimit_path: Path = Path("/usr/bin/prlimit")
    bash_path: Path = Path("/bin/bash")
    bubblewrap_min_version: str = "0.11.2"
    tmpfs_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int = 300
    max_processes: int = 64
    max_open_files: int = 512
    max_file_size_bytes: int = 1024 * 1024 * 1024
    max_output_bytes: int = 8 * 1024 * 1024
    max_parallel_execs_per_sandbox: int = Field(default=16, ge=1, le=64)
    telemetry_sink: str | None = Field(default=None)
    credential_broker: str = "disabled"
    max_file_api_bytes: int = 32 * 1024 * 1024
    # A directory listing is paged, but the page itself is bounded so one call
    # on a node_modules tree cannot build a million-entry response in memory.
    max_list_entries: int = Field(default=1000, ge=1, le=10_000)
    default_timeout_seconds: int = 300
    terminate_grace_seconds: float = 5.0
    readonly_mounts: CommaSeparatedList = Field(
        default_factory=lambda: ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc"]
    )

    http_proxies: CommaSeparatedList = Field(default_factory=list)
    no_proxy: str = "127.0.0.1,localhost,::1"
    egress_denied_addresses: CommaSeparatedList = Field(default_factory=list)
    egress_allowed_literals: CommaSeparatedList = Field(default_factory=list)
    python_index_url: str = "https://pypi.org/simple/"
    npm_registry: str = "https://registry.npmjs.org"
    go_proxy: str = "https://proxy.golang.org,direct"
    go_sumdb: str = "sum.golang.org"
    cargo_registry_url: str = ""
    maven_repository_url: str = ""
    java_home: str | None = None
    rust_launcher_dir: str | None = None
    toolchains: CommaSeparatedList = Field(default_factory=lambda: ["python", "node"])
    mcp_allowed_hosts: CommaSeparatedList = Field(
        default_factory=lambda: [
            "localhost",
            "localhost:*",
            "127.0.0.1",
            "127.0.0.1:*",
            "test",
        ]
    )

    blobstore_endpoint: str = Field(default_factory=lambda: os.getenv("BLOBSTORE_ENDPOINT", ""))
    blobstore_bucket: str = Field(default_factory=lambda: os.getenv("BLOBSTORE_BUCKET", ""))
    blobstore_base_prefix: str = Field(default_factory=lambda: _blobstore_base_prefix())
    blobstore_region: str = Field(
        default_factory=lambda: os.getenv("BLOBSTORE_REGION", "us-east-1")
    )
    object_store_backend: str = "s3"

    # Environment templates are shared read-only across every sandbox on a
    # worker, so the cache lives beside the sandboxes rather than inside any one
    # of them. `None` derives it from the workspace root.
    template_root: Path | None = None
    template_cache_max_bytes: int | None = None
    # A single template that expands past this is almost certainly a mistake
    # (someone snapshotted an entire workspace), and the cache disk is shared.
    template_max_extract_bytes: int = 8 * 1024 * 1024 * 1024
    template_max_archive_bytes: int = 4 * 1024 * 1024 * 1024
    template_snapshot_max_entries: int = Field(default=200_000, ge=1)
    template_snapshot_max_depth: int = Field(default=128, ge=1, le=256)
    template_snapshot_timeout_seconds: float = Field(default=600.0, gt=0, allow_inf_nan=False)

    @field_validator("shared_root", mode="before")
    @classmethod
    def empty_shared_root_is_none(cls, value: object) -> object:
        return None if value in {None, ""} else value

    @field_validator(
        "readonly_mounts",
        "http_proxies",
        "mcp_allowed_hosts",
        "toolchains",
        "egress_denied_addresses",
        "egress_allowed_literals",
        "isolation_required_features",
        "isolation_optional_features",
        mode="before",
    )
    @classmethod
    def split_string_lists(cls, value: object) -> object:
        """Accept `a,b,c`, a single value, or a JSON array.

        The comma form is the one an operator writes, and it is what this
        validator always intended. It only ever failed because pydantic-settings
        decoded the environment value as JSON first — see `CommaSeparatedList`.

        The JSON form is still accepted: it is what worked before, deployments
        may be using it, and silently splitting `["python","go"]` on its commas
        would turn a working configuration into a confusing "unknown toolchain
        '[\"python\"'" error.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                # Fall through to comma splitting so the value reaches the
                # real validator and is reported against the field, rather than
                # failing here as an opaque parse error.
                pass
            else:
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]

    @field_validator("toolchains")
    @classmethod
    def validate_toolchains(cls, value: list[str]) -> list[str]:
        """Reject an unknown toolchain instead of silently omitting its environment.

        A deployment that asks for `rust` and gets no `CARGO_HOME` would only
        find out when a build writes into the wrong directory, so the name is
        checked here rather than at execution time. Order is preserved because
        it decides PATH precedence; duplicates are dropped.
        """

        normalized: list[str] = []
        for item in value:
            name = item.strip().lower()
            if not name:
                continue
            if name not in SUPPORTED_TOOLCHAINS:
                supported = ", ".join(SUPPORTED_TOOLCHAINS)
                raise ValueError(f"unknown toolchain {item!r}; supported: {supported}")
            if name not in normalized:
                normalized.append(name)
        if not normalized:
            raise ValueError("at least one toolchain must be enabled")
        return normalized

    @field_validator("egress_denied_addresses")
    @classmethod
    def validate_denied_addresses(cls, value: list[str]) -> list[str]:
        """Reject a malformed entry rather than silently dropping a deny rule."""

        for item in value:
            entry = item.strip()
            if "/" in entry:
                ipaddress.ip_network(entry, strict=False)
            else:
                ipaddress.ip_address(entry)
        return [item.strip() for item in value if item.strip()]

    @field_validator("egress_allowed_literals")
    @classmethod
    def validate_allowed_literals(cls, value: list[str]) -> list[str]:
        for item in value:
            ipaddress.ip_address(item.strip())
        return [item.strip() for item in value if item.strip()]

    @field_validator("isolation_level")
    @classmethod
    def validate_isolation_level(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != "auto":
            IsolationLevel.parse(normalized)
        return normalized

    @field_validator("network_mode")
    @classmethod
    def validate_network_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"host", "isolated"}:
            raise ValueError("network_mode must be 'host' or 'isolated'")
        return normalized

    @field_validator("suspend_snapshot")
    @classmethod
    def validate_suspend_snapshot(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "always", "never"}:
            raise ValueError("suspend_snapshot must be 'auto', 'always' or 'never'")
        return normalized

    @field_validator("isolation_required_features", "isolation_optional_features")
    @classmethod
    def validate_isolation_features(cls, value: list[str]) -> list[str]:
        normalized = sorted({item.strip().lower() for item in value if item.strip()})
        unknown = set(normalized) - {"pid_namespace", "cgroup_namespace"}
        if unknown:
            raise ValueError(f"unsupported isolation features: {sorted(unknown)}")
        return normalized

    @field_validator(
        "execution_backend",
        "metadata_backend",
        "registry_backend",
        "object_store_backend",
        "credential_broker",
    )
    @classmethod
    def validate_backend_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("backend name cannot be empty")
        return normalized

    @property
    def workspace_root(self) -> Path:
        return self.shared_root or self.local_root

    @property
    def template_cache_root(self) -> Path:
        """Where materialized templates live on this worker.

        A sibling of the sandbox root rather than a child, so `_validate_top_level`
        never sees it and a sandbox can never reach it through its own tree.
        """
        if self.template_root is not None:
            return self.template_root
        return self.workspace_root.parent / "templates"

    @property
    def worker_endpoint(self) -> str:
        return f"{self.advertise_scheme}://{self.advertise_host}:{self.port}"

    def resolved_database_url(self) -> str:
        return self.database_url or "sqlite+aiosqlite:///./data/agent-sandbox.db"


def get_environment() -> str:
    configured = os.getenv("SANDBOX_ENVIRONMENT", "local").strip().lower()
    if configured not in {"local", "staging", "prod"}:
        raise RuntimeError("SANDBOX_ENVIRONMENT must be local, staging, or prod")
    return configured


def get_environment_type() -> str:
    """Return a deployment-safe environment label for health diagnostics."""
    environment = get_environment().upper()
    zone = os.getenv("SANDBOX_ZONE", "").strip()
    region = os.getenv("SANDBOX_REGION", "").strip()
    return "-".join(part for part in (environment, zone, region) if part)


def _normalize_prefix(value: str) -> str:
    normalized = value.strip().strip("/")
    return f"{normalized}/" if normalized else ""


def _blobstore_base_prefix() -> str:
    return _normalize_prefix(os.getenv("BLOBSTORE_BASE_PREFIX", "agent-sandbox/"))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
