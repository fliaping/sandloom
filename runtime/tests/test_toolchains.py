"""Tests for per-language toolchain composition."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_sandbox_runtime import (
    SUPPORTED_TOOLCHAINS,
    InteractiveEnvironment,
    InteractiveSandboxCommandBuilder,
    IsolationLevel,
    SandboxRuntimeProfile,
    Toolchain,
    build_toolchain,
    compose_environment,
    go_toolchain,
    java_toolchain,
    node_toolchain,
    python_toolchain,
    rust_toolchain,
    toolchains_unavailable_at,
)
from agent_sandbox_runtime.toolchains import reserved_environment


def _profile() -> SandboxRuntimeProfile:
    return SandboxRuntimeProfile(
        bubblewrap_path=Path("/opt/bwrap"),
        setpriv_path=Path("/opt/setpriv"),
        prlimit_path=Path("/opt/prlimit"),
        readonly_mounts=("/usr",),
    )


def _env_of(command: list[str]) -> dict[str, str]:
    """Extract the --setenv pairs from a built Bubblewrap argv."""

    env: dict[str, str] = {}
    for index, token in enumerate(command):
        if token == "--setenv":
            env[command[index + 1]] = command[index + 2]
    return env


def test_every_supported_toolchain_builds_by_name() -> None:
    options = {
        "index_url": "https://pypi.example/simple/",
        "registry": "https://npm.example",
        "proxy": "https://goproxy.example",
        "registry_url": "https://crates.example",
        "maven_repository_url": "https://maven.example",
    }

    for name in SUPPORTED_TOOLCHAINS:
        toolchain = build_toolchain(name, **options)
        assert toolchain.name == name
        assert toolchain.environment


def test_unknown_toolchain_is_rejected_with_the_supported_list() -> None:
    with pytest.raises(ValueError, match="unknown toolchain"):
        build_toolchain("cobol")


def test_go_toolchain_separates_installed_binaries_from_build_cache() -> None:
    toolchain = go_toolchain(proxy="https://goproxy.example")

    # GOPATH holds `go install` output, which a template should be able to
    # supply, so it lives in /envs. The caches are per-sandbox scratch.
    assert toolchain.environment["GOPATH"] == "/envs/go"
    assert toolchain.environment["GOMODCACHE"] == "/cache/go/mod"
    assert toolchain.environment["GOCACHE"] == "/cache/go/build"
    assert toolchain.environment["GOPROXY"] == "https://goproxy.example"
    # The image's own `go` has to be on PATH as well. The runtime builds PATH
    # from these entries, so the image's ENV PATH does not survive; a toolchain
    # that lists only /envs exposes the binaries a sandbox installed and hides
    # the compiler the image shipped.
    assert toolchain.path_entries == ("/envs/go/bin", "/usr/local/go/bin")


def test_go_toolchain_bounds_compiler_parallelism() -> None:
    """An unbounded build exceeds the sandbox's process limit.

    Go forks one `compile` per core. The default RLIMIT_NPROC is 64, so on a
    many-core host the build dies with "fork/exec ...: resource temporarily
    unavailable" — a failure that looks like a broken toolchain rather than a
    resource limit.
    """
    toolchain = go_toolchain(proxy="https://goproxy.example", max_parallel_compiles=7)

    assert toolchain.environment["GOMAXPROCS"] == "7"

    # Bounded by default, so a deployment that reads none of this still builds.
    assert int(go_toolchain(proxy="p").environment["GOMAXPROCS"]) < 64


def test_rust_toolchain_keeps_cargo_home_writable_and_targets_cache() -> None:
    toolchain = rust_toolchain(registry_url="https://crates.example")

    assert toolchain.environment["CARGO_HOME"] == "/envs/cargo"
    assert toolchain.environment["CARGO_TARGET_DIR"] == "/cache/cargo-target"
    assert toolchain.environment["CARGO_REGISTRIES_CRATES_IO_INDEX"] == "https://crates.example"


def test_rust_toolchain_keeps_rustup_home_off_the_sandbox() -> None:
    """`cargo` and `rustc` on PATH are rustup shims.

    They find their toolchains under RUSTUP_HOME. Pointing that at a
    per-sandbox /envs/rustup — which starts empty — hides every toolchain the
    image installed, and `rustc` answers "could not choose a version of rustc to
    run". The image's copy is shared and read-only, so it is addressed at the
    image path; only CARGO_HOME is per-sandbox, because `cargo install` writes
    there.
    """
    toolchain = rust_toolchain()

    assert toolchain.environment["RUSTUP_HOME"] == "/usr/local/rustup"
    assert not toolchain.environment["RUSTUP_HOME"].startswith("/envs/")
    # The image's shims have to be reachable, for the same reason Go's do.
    assert toolchain.path_entries == ("/envs/cargo/bin", "/usr/local/cargo/bin")


def test_rust_toolchain_omits_registry_when_unset() -> None:
    assert "CARGO_REGISTRIES_CRATES_IO_INDEX" not in rust_toolchain().environment


def test_java_toolchain_points_maven_and_gradle_at_the_sandbox_cache() -> None:
    toolchain = java_toolchain()

    assert "-Dmaven.repo.local=/cache/maven" in toolchain.environment["MAVEN_OPTS"]
    assert toolchain.environment["GRADLE_USER_HOME"] == "/cache/gradle"
    # JAVA_HOME is the deployment's to set: only it knows which JDK the image
    # installed.
    assert "JAVA_HOME" not in toolchain.environment


def test_procfs_free_java_home_is_used_for_launchers_and_build_tools() -> None:
    toolchain = java_toolchain(java_home="/usr/local/lib/agent-sandbox/java")
    assert toolchain.environment["JAVA_HOME"] == "/usr/local/lib/agent-sandbox/java"
    assert toolchain.path_entries[-1] == "/usr/local/lib/agent-sandbox/java/bin"
    assert not toolchain.needs_procfs
    assert toolchains_unavailable_at(IsolationLevel.BASIC.features, [toolchain]) == ()


def test_procfs_free_rust_launchers_precede_rustup_proxies() -> None:
    toolchain = rust_toolchain(launcher_dir="/usr/local/lib/agent-sandbox/rust/bin")
    assert toolchain.path_entries == (
        "/envs/cargo/bin",
        "/usr/local/lib/agent-sandbox/rust/bin",
        "/usr/local/cargo/bin",
    )
    assert not toolchain.needs_procfs
    assert toolchains_unavailable_at(IsolationLevel.BASIC.features, [toolchain]) == ()


def test_compose_rejects_two_toolchains_that_disagree_on_one_variable() -> None:
    first = Toolchain(name="first", environment={"SHARED": "a"})
    second = Toolchain(name="second", environment={"SHARED": "b"})

    with pytest.raises(ValueError, match="both define SHARED"):
        compose_environment([first, second], base_path=())


def test_compose_allows_two_toolchains_that_agree() -> None:
    first = Toolchain(name="first", environment={"SHARED": "same"})
    second = Toolchain(name="second", environment={"SHARED": "same"})

    environment, _ = compose_environment([first, second], base_path=())

    assert environment["SHARED"] == "same"


def test_compose_keeps_toolchain_path_before_the_system_path() -> None:
    _, path_entries = compose_environment(
        [go_toolchain(proxy="https://goproxy.example"), rust_toolchain()],
        base_path=("/usr/bin", "/bin"),
    )

    assert path_entries == (
        "/envs/go/bin",
        "/usr/local/go/bin",
        "/envs/cargo/bin",
        "/usr/local/cargo/bin",
        "/usr/bin",
        "/bin",
    )
    # Every toolchain entry still precedes the system path, so a sandbox's own
    # install shadows the image's and the image's shadows the base system's.
    assert path_entries.index("/usr/local/go/bin") < path_entries.index("/usr/bin")


def test_compose_deduplicates_path_entries() -> None:
    shared = Toolchain(name="shared", path_entries=("/envs/shared/bin", "/usr/bin"))

    _, path_entries = compose_environment([shared], base_path=("/usr/bin", "/bin"))

    assert path_entries == ("/envs/shared/bin", "/usr/bin", "/bin")


def test_reserved_environment_covers_managed_names_and_prefixes() -> None:
    names, prefixes = reserved_environment(
        [go_toolchain(proxy="https://goproxy.example"), rust_toolchain()]
    )

    # Every variable a toolchain sets is reserved, because a caller that could
    # override it would change where a build reads and writes.
    assert "GOPATH" in names
    assert "CARGO_HOME" in names
    assert "GO" in prefixes
    assert "CARGO_" in prefixes


def test_polyglot_environment_reaches_the_built_command() -> None:
    command = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            toolchains=(
                python_toolchain(index_url="https://pypi.example/simple/"),
                node_toolchain(registry="https://npm.example"),
                go_toolchain(proxy="https://goproxy.example"),
                rust_toolchain(),
                java_toolchain(),
            ),
        ),
    ).build(
        sandbox_root=Path("/sandboxes/s1"),
        sandbox_uid=20001,
        argv=("/bin/bash", "-lc", "go build ./..."),
        cwd="/workspace",
        user_env={},
    )
    env = _env_of(command)

    assert env["GOPROXY"] == "https://goproxy.example"
    assert env["CARGO_HOME"] == "/envs/cargo"
    assert env["GRADLE_USER_HOME"] == "/cache/gradle"
    assert env["PIP_INDEX_URL"] == "https://pypi.example/simple/"
    assert env["npm_config_registry"] == "https://npm.example"
    for entry in ("/envs/go/bin", "/envs/cargo/bin", "/envs/npm-global/bin"):
        assert entry in env["PATH"].split(":")


def test_a_caller_cannot_override_another_language_build_environment() -> None:
    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            toolchains=(
                go_toolchain(proxy="https://goproxy.example"),
                rust_toolchain(),
                java_toolchain(),
            ),
        ),
    )

    # Each of these redirects where a build fetches from or writes to, so none
    # of them may come from the caller.
    for name in ("GOPROXY", "GOFLAGS", "GOPATH", "CARGO_HOME", "RUSTFLAGS", "MAVEN_OPTS"):
        with pytest.raises(ValueError, match="sandbox-managed"):
            builder.build(
                sandbox_root=Path("/sandboxes/s1"),
                sandbox_uid=20001,
                argv=("/bin/true",),
                cwd="/workspace",
                user_env={name: "attacker-controlled"},
            )


def test_a_disabled_toolchain_leaves_its_variables_unreserved() -> None:
    """A deployment without Go has no reason to refuse GOPROXY.

    The reserved set is derived from the enabled toolchains, so it tracks what
    the sandbox actually manages instead of a fixed list.
    """

    builder = InteractiveSandboxCommandBuilder(
        _profile(),
        InteractiveEnvironment(
            python_index_url="https://pypi.example/simple/",
            npm_registry="https://npm.example",
            toolchains=(python_toolchain(index_url="https://pypi.example/simple/"),),
        ),
    )

    command = builder.build(
        sandbox_root=Path("/sandboxes/s1"),
        sandbox_uid=20001,
        argv=("/bin/true",),
        cwd="/workspace",
        user_env={"GOPROXY": "https://caller.example"},
    )

    assert _env_of(command)["GOPROXY"] == "https://caller.example"


def test_default_toolchains_preserve_the_python_and_node_environment() -> None:
    """An existing deployment that never configures toolchains is unchanged."""

    environment = InteractiveEnvironment(
        python_index_url="https://pypi.example/simple/",
        npm_registry="https://npm.example",
        platform_python_runtime=Path("/platform/.venv"),
        platform_runtime_packages=Path("/platform/runtime"),
    )

    names = [toolchain.name for toolchain in environment.resolved_toolchains()]

    assert names == ["python", "node"]


def test_the_feature_list_that_gates_a_toolchain_is_the_one_a_level_reports() -> None:
    """The rule and the list it reads are produced in different modules.

    `toolchains_unavailable_at` matches the literal string "private_procfs", and
    the list it is handed in production is `IsolationLevel.features`, which the
    same module also publishes as the API's advertised capabilities. The
    control-plane tests build that list by hand, so a level that stopped
    advertising the feature — or a rename on either side — would leave every
    check green while the startup warning for a JVM or Rust build quietly
    stopped firing. This joins the two halves.
    """

    rust = rust_toolchain()

    assert "private_procfs" in IsolationLevel.STANDARD.features
    assert toolchains_unavailable_at(IsolationLevel.STANDARD.features, [rust]) == ()
    assert toolchains_unavailable_at(IsolationLevel.BASIC.features, [rust]) == ("rust",)


def test_each_isolation_level_advertises_every_feature_of_the_weaker_one() -> None:
    """A stronger level only adds namespaces, so the lists are cumulative.

    The `strict` branch is what an operator reads in `/capabilities` after the
    container was granted a cgroup namespace, and it is the only place
    `cgroup_namespace` is advertised.
    """

    basic = set(IsolationLevel.BASIC.features)
    standard = set(IsolationLevel.STANDARD.features)
    strict = set(IsolationLevel.STRICT.features)

    assert basic < standard < strict
    assert "cgroup_namespace" in strict - standard
    assert "private_procfs" in standard - basic


def test_a_toolchain_without_a_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="name cannot be empty"):
        Toolchain(name="  ")
