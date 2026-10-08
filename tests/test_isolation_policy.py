from __future__ import annotations

import pytest
from agent_sandbox_runtime import IsolationLevel, SandboxRuntimeProfile

from agent_sandbox.config import Settings
from agent_sandbox.isolation_policy import negotiate_features


async def test_optional_failure_never_removes_required_feature() -> None:
    seen = []

    async def probe(features: tuple[str, ...]) -> tuple[bool, str]:
        seen.append(features)
        return ("pid_namespace" not in features, "procfs masked")

    result = await negotiate_features(
        IsolationLevel.BASIC.features, ["cgroup_namespace"], ["pid_namespace"], probe
    )
    assert result.enabled == ("cgroup_namespace",)
    assert result.skipped == {"pid_namespace": "procfs masked"}
    assert seen == [("cgroup_namespace",), ("cgroup_namespace", "pid_namespace")]


async def test_required_feature_failure_is_fatal() -> None:
    async def probe(features: tuple[str, ...]) -> tuple[bool, str]:
        return False, "cgroup denied"

    with pytest.raises(RuntimeError, match="required isolation features"):
        await negotiate_features([], ["cgroup_namespace"], [], probe)


async def test_baseline_guarantees_are_not_optional() -> None:
    async def probe(features: tuple[str, ...]) -> tuple[bool, str]:
        raise AssertionError("already provided by baseline")

    result = await negotiate_features(
        IsolationLevel.STRICT.features, [], ["pid_namespace", "cgroup_namespace"], probe
    )
    assert result.enabled == ()
    assert result.skipped == {}


async def test_unknown_feature_fails_closed() -> None:
    async def probe(features: tuple[str, ...]) -> tuple[bool, str]:
        raise AssertionError("must reject before probe")

    with pytest.raises(ValueError, match="unsupported"):
        await negotiate_features([], [], ["memory_quota"], probe)


def test_settings_parse_comma_and_json_feature_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SANDBOX_ISOLATION_REQUIRED_FEATURES", "cgroup_namespace")
    monkeypatch.setenv("SANDBOX_ISOLATION_OPTIONAL_FEATURES", '["pid_namespace"]')
    settings = Settings(_env_file=None)
    assert settings.isolation_required_features == ["cgroup_namespace"]
    assert settings.isolation_optional_features == ["pid_namespace"]


def test_custom_profile_is_distinct_without_mislabeling_strict() -> None:
    base = SandboxRuntimeProfile()
    custom = SandboxRuntimeProfile(additional_features=("cgroup_namespace",))
    custom.validate()
    assert custom.isolation_level is IsolationLevel.BASIC
    assert "cgroup_namespace" in custom.features
    assert "pid_namespace" not in custom.features
    assert custom.effective_profile_hash != base.effective_profile_hash


def test_pid_capability_always_includes_private_procfs() -> None:
    profile = SandboxRuntimeProfile(additional_features=("pid_namespace",))
    assert {"pid_namespace", "private_procfs"} <= set(profile.features)


def test_nested_userns_fallback_has_a_distinct_profile() -> None:
    assert SandboxRuntimeProfile(disable_nested_userns=False).effective_profile_hash != (
        SandboxRuntimeProfile().effective_profile_hash
    )


def test_profile_hash_normalizes_feature_order_and_duplicates() -> None:
    first = SandboxRuntimeProfile(additional_features=("pid_namespace", "cgroup_namespace"))
    second = SandboxRuntimeProfile(
        additional_features=("cgroup_namespace", "pid_namespace", "pid_namespace")
    )
    assert first.features == second.features
    assert first.effective_profile_hash == second.effective_profile_hash


def test_baseline_features_do_not_create_a_different_hash() -> None:
    strict = SandboxRuntimeProfile(isolation_level=IsolationLevel.STRICT)
    redundant = SandboxRuntimeProfile(
        isolation_level=IsolationLevel.STRICT,
        additional_features=("pid_namespace", "cgroup_namespace"),
    )
    assert redundant.effective_profile_hash == strict.effective_profile_hash
