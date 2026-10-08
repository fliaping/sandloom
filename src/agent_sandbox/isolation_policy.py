"""Additive feature negotiation; baseline Level guarantees cannot be removed."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

_SUPPORTED = {"pid_namespace", "cgroup_namespace"}


@dataclass(frozen=True, slots=True)
class FeatureSelection:
    enabled: tuple[str, ...]
    skipped: dict[str, str]


async def negotiate_features(
    baseline: Iterable[str],
    required: Iterable[str],
    optional: Iterable[str],
    probe: Callable[[tuple[str, ...]], Awaitable[tuple[bool, str]]],
) -> FeatureSelection:
    """Probe the complete combination, never just independent feature flags.

    Optional capabilities are tried in deterministic name order against the
    already accepted combination. No failure removes a required feature.
    """
    base = set(baseline)
    required_set, optional_set = set(required), set(optional)
    unknown = (required_set | optional_set) - _SUPPORTED
    if unknown:
        raise ValueError(f"unsupported isolation features: {sorted(unknown)}")
    enabled = required_set - base
    if enabled:
        ok, detail = await probe(tuple(sorted(enabled)))
        if not ok:
            raise RuntimeError(
                f"required isolation features {sorted(enabled)} unavailable: {detail}"
            )
    skipped = {}
    for feature in sorted(optional_set - required_set - base):
        proposed = tuple(sorted(enabled | {feature}))
        ok, detail = await probe(proposed)
        if ok:
            enabled.add(feature)
        else:
            skipped[feature] = detail
    return FeatureSelection(tuple(sorted(enabled)), skipped)
