"""Isolation policy and capability negotiation for Bubblewrap runtimes."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import IntEnum


class IsolationLevel(IntEnum):
    """Monotonic isolation levels exposed by the public runtime API.

    ``BASIC`` is the portable baseline. ``STANDARD`` adds PID isolation and a
    private procfs. ``STRICT`` additionally requires a cgroup namespace.
    Network isolation is deliberately a separate policy because disabling
    egress changes application behaviour rather than only strengthening the
    process boundary.
    """

    BASIC = 1
    STANDARD = 2
    STRICT = 3

    @classmethod
    def parse(cls, value: str | IsolationLevel) -> IsolationLevel:
        if isinstance(value, IsolationLevel):
            return value
        normalized = value.strip().lower()
        aliases = {
            "1": cls.BASIC,
            "basic": cls.BASIC,
            "2": cls.STANDARD,
            "standard": cls.STANDARD,
            "3": cls.STRICT,
            "strict": cls.STRICT,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown isolation level: {value!r}") from exc

    @property
    def label(self) -> str:
        return self.name.lower()

    @property
    def features(self) -> tuple[str, ...]:
        features = [
            "resource_limits",
            "no_new_privileges",
            "uid_separation",
            "user_namespace",
            "mount_namespace",
            "ipc_namespace",
            "uts_namespace",
            "private_tmp",
            "readonly_system_mounts",
        ]
        if self >= IsolationLevel.STANDARD:
            features.extend(("pid_namespace", "private_procfs"))
        if self >= IsolationLevel.STRICT:
            features.append("cgroup_namespace")
        return tuple(features)


@dataclass(frozen=True, slots=True)
class IsolationSelection:
    requested: str
    selected: IsolationLevel
    supported: tuple[IsolationLevel, ...]
    failures: dict[str, str]

    @property
    def max_supported(self) -> IsolationLevel:
        return max(self.supported)

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_level": self.requested,
            "selected_level": self.selected.label,
            "max_supported_level": self.max_supported.label,
            "supported_levels": [level.label for level in self.supported],
            "features": list(self.selected.features),
            "probe_failures": self.failures,
        }


def negotiate_isolation(
    requested: str,
    probe: Callable[[IsolationLevel], tuple[bool, str]],
    *,
    candidates: Iterable[IsolationLevel] | None = None,
) -> IsolationSelection:
    """Probe levels and select the strongest supported policy.

    ``auto`` falls back monotonically. An explicit level is strict: startup
    fails if the host/container cannot provide every feature in that level.
    """

    ordered = tuple(
        sorted(
            candidates or tuple(IsolationLevel),
            reverse=True,
        )
    )
    results: dict[IsolationLevel, bool] = {}
    failures: dict[str, str] = {}
    for level in ordered:
        ok, detail = probe(level)
        results[level] = ok
        if not ok:
            failures[level.label] = detail

    supported = tuple(sorted(level for level, ok in results.items() if ok))
    if not supported:
        detail = "; ".join(f"{key}={value}" for key, value in failures.items())
        raise RuntimeError(f"no supported Bubblewrap isolation level: {detail}")

    normalized = requested.strip().lower()
    if normalized == "auto":
        selected = max(supported)
    else:
        selected = IsolationLevel.parse(normalized)
        if selected not in supported:
            reason = failures.get(selected.label, "capability probe failed")
            raise RuntimeError(
                f"requested isolation level {selected.label!r} is unavailable: {reason}"
            )
    return IsolationSelection(
        requested=normalized,
        selected=selected,
        supported=supported,
        failures=failures,
    )


__all__ = ["IsolationLevel", "IsolationSelection", "negotiate_isolation"]
