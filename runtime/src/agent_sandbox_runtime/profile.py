"""Versioned, application-neutral Sandbox runtime profile."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .isolation import IsolationLevel


@dataclass(frozen=True, slots=True)
class SandboxRuntimeProfile:
    """The security and resource contract shared by every Sandbox consumer."""

    profile_id: str = "coding-default"
    profile_hash: str = "bubblewrap-0.11.2-generic-v12"
    bubblewrap_path: Path = Path("/usr/bin/bwrap")
    setpriv_path: Path = Path("/usr/bin/setpriv")
    prlimit_path: Path = Path("/usr/bin/prlimit")
    minimum_version: str = "0.11.2"
    isolation_level: IsolationLevel = IsolationLevel.BASIC
    additional_features: tuple[str, ...] = ()
    network_isolated: bool = False
    disable_nested_userns: bool = True
    root_drop_uid: int = 65534
    tmpfs_bytes: int = 512 * 1024 * 1024
    cpu_seconds: int | None = 300
    max_processes: int | None = 64
    max_open_files: int = 512
    max_file_size_bytes: int = 1024 * 1024 * 1024
    readonly_mounts: tuple[str, ...] = (
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/etc",
    )

    def validate(self) -> None:
        unknown = set(self.additional_features) - {"pid_namespace", "cgroup_namespace"}
        if unknown:
            raise ValueError(f"unknown additional isolation features: {sorted(unknown)}")
        if not self.profile_id.strip():
            raise ValueError("Sandbox profile_id cannot be empty")
        if not self.profile_hash.strip():
            raise ValueError("Sandbox profile_hash cannot be empty")
        if self.root_drop_uid < 1:
            raise ValueError("Sandbox root_drop_uid must be positive")
        for name, value in (
            ("tmpfs_bytes", self.tmpfs_bytes),
            ("max_open_files", self.max_open_files),
            ("max_file_size_bytes", self.max_file_size_bytes),
        ):
            if value < 1:
                raise ValueError(f"Sandbox {name} must be positive")
        if self.cpu_seconds is not None and self.cpu_seconds < 1:
            raise ValueError("Sandbox cpu_seconds must be positive")
        if self.max_processes is not None and self.max_processes < 1:
            raise ValueError("Sandbox max_processes must be positive")

    @property
    def features(self) -> tuple[str, ...]:
        features = set(self.isolation_level.features) | set(self.additional_features)
        if "pid_namespace" in features:
            features.add("private_procfs")
        return tuple(sorted(features))

    @property
    def effective_profile_hash(self) -> str:
        network = "net-none" if self.network_isolated else "net-host"
        suffix = f"-{self.isolation_level.label}-{network}"
        extras = sorted(set(self.additional_features) - set(self.isolation_level.features))
        if extras:
            suffix += "-with-" + "-".join(extras)
        if not self.disable_nested_userns:
            suffix += "-nested-userns"
        return (
            self.profile_hash
            if self.profile_hash.endswith(suffix)
            else f"{self.profile_hash}{suffix}"
        )


__all__ = ["SandboxRuntimeProfile"]
