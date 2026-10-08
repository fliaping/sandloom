"""Per-language toolchain environments for sandboxes.

A sandbox is a POSIX workspace running arbitrary argv, so the runtime itself is
language-neutral. What a language *does* need is a place to put its package
cache, a registry to fetch from, and a bin directory on ``PATH``. Hardcoding
one language's variables into the managed environment makes every other
language a second-class citizen, so each one is described here instead and the
deployment composes the set it wants.

Two invariants hold for every toolchain:

* Caches live under ``/cache/<tool>`` so they persist with the sandbox and are
  never shared across tenants.
* User-installed binaries live under ``/envs/<tool>`` so a prebuilt template
  can supply them read-only.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

CACHE_ROOT = "/cache"
# Where the polyglot base image installs Go and Rust. These are image paths,
# not sandbox paths, and they are parameters so a deployment that installs a
# language elsewhere says so instead of editing this file. A sandbox reaches
# them through the read-only /usr mount it already has.
DEFAULT_GO_INSTALL_ROOT = "/usr/local/go"
# Rust is different: rustup installs its shims into the image's CARGO_HOME,
# not into /usr/local/bin, so that directory — not the image prefix — is what
# has to be on PATH.
DEFAULT_RUST_SHIM_DIR = "/usr/local/cargo/bin"
DEFAULT_RUSTUP_HOME = "/usr/local/rustup"
ENVS_ROOT = "/envs"
WORKSPACE_ROOT = "/workspace"
HOME_ROOT = "/home/sandbox"


@dataclass(frozen=True, slots=True)
class Toolchain:
    """One language's managed environment contribution.

    ``name`` identifies the toolchain in configuration and diagnostics.
    ``environment`` is applied to every execution. ``path_entries`` are
    prepended to ``PATH`` in toolchain order. Cache directories are not
    declared here: ``/cache`` and ``/envs`` are bind-mounted over whatever the
    sandbox already has, so a tool creates its own subdirectory on first use.
    ``reserved_names`` and ``reserved_prefixes`` are refused from user-supplied
    environments, because a caller that can set ``GOFLAGS`` or ``MAVEN_OPTS``
    can change what a build does.
    """

    name: str
    environment: Mapping[str, str] = field(default_factory=dict)
    path_entries: Sequence[str] = ()
    reserved_names: Sequence[str] = ()
    reserved_prefixes: Sequence[str] = ()
    # Whether the language's own tools need `/proc/self/exe`.
    #
    # `basic` isolation deliberately mounts no procfs, so an ELF binary cannot
    # ask the kernel where it lives. A launcher that resolves `$ORIGIN/../lib`
    # then cannot find its own shared libraries, which surfaces as
    # "libjli.so: cannot open shared object file" for the JDK and
    # "librustc_driver-....so: cannot open shared object file" for Rust — a
    # linker error for a toolchain that is installed perfectly well. Declaring
    # it here lets the service say so at startup instead of leaving the
    # operator to read it out of a compiler's error message.
    needs_procfs: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("toolchain name cannot be empty")


def python_toolchain(
    *,
    index_url: str,
    platform_python_runtime: str | None = None,
    platform_runtime_packages: str | None = None,
) -> Toolchain:
    """CPython with pip and uv.

    ``UV_PYTHON_DOWNLOADS=never`` keeps uv on the interpreter the image ships
    instead of silently fetching another one over the network.
    """

    environment = {
        "PIP_CACHE_DIR": f"{CACHE_ROOT}/pip",
        "PIP_INDEX_URL": index_url,
        "UV_INDEX_URL": index_url,
        "UV_CACHE_DIR": f"{CACHE_ROOT}/uv",
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PROJECT_ENVIRONMENT": f"{WORKSPACE_ROOT}/.venv",
        "UV_TOOL_DIR": f"{ENVS_ROOT}/uv-tools",
        "UV_TOOL_BIN_DIR": f"{ENVS_ROOT}/uv-tools/bin",
        "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    if platform_runtime_packages is not None:
        environment["PYTHONPATH"] = platform_runtime_packages
    path_entries: list[str] = []
    if platform_python_runtime is not None:
        path_entries.append(f"{platform_python_runtime}/bin")
    path_entries.extend(
        (
            f"{ENVS_ROOT}/uv-tools/bin",
            f"{ENVS_ROOT}/python-venv/bin",
        )
    )
    return Toolchain(
        name="python",
        environment=environment,
        path_entries=tuple(path_entries),
        reserved_names=(
            "PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONNOUSERSITE",
            "PYTHONPATH",
        ),
        reserved_prefixes=("PIP_", "UV_"),
    )


def node_toolchain(*, registry: str) -> Toolchain:
    """Node.js with npm, pnpm, and yarn.

    TypeScript needs no toolchain of its own: ``tsc`` and the runners that
    execute TypeScript directly are npm packages, so they install into these
    same directories.
    """

    return Toolchain(
        name="node",
        environment={
            "npm_config_cache": f"{CACHE_ROOT}/npm",
            "npm_config_registry": registry,
            "npm_config_prefix": f"{ENVS_ROOT}/npm-global",
            "npm_config_store_dir": f"{CACHE_ROOT}/pnpm",
            "PNPM_HOME": f"{ENVS_ROOT}/pnpm",
            "YARN_CACHE_FOLDER": f"{CACHE_ROOT}/yarn",
            "YARN_NPM_REGISTRY_SERVER": registry,
        },
        path_entries=(f"{ENVS_ROOT}/npm-global/bin", f"{ENVS_ROOT}/pnpm"),
        reserved_names=("PNPM_HOME",),
        reserved_prefixes=("NPM_CONFIG_", "PNPM_", "YARN_"),
    )


def go_toolchain(
    *,
    proxy: str,
    sumdb: str = "sum.golang.org",
    install_root: str = DEFAULT_GO_INSTALL_ROOT,
    max_parallel_compiles: int = 4,
) -> Toolchain:
    """Go with a module proxy.

    ``GOFLAGS=-mod=mod`` is deliberately not set: the default (``-mod=readonly``
    when a ``go.mod`` is present) is the safer one. ``GOPATH`` lands in
    ``/envs`` rather than the home directory so ``go install`` output survives
    in a template, while the build and module caches stay in ``/cache``.
    """

    environment = {
        # The compiler forks one `compile` process per core and the sandbox has
        # a process limit (RLIMIT_NPROC, default 64), so an unconstrained run on
        # a many-core host fails with "fork/exec ...: resource temporarily
        # unavailable" rather than compiling slowly. Bounding it here keeps the
        # default configuration working; a deployment that raises
        # SANDBOX_MAX_PROCESSES can raise this too.
        "GOMAXPROCS": str(max_parallel_compiles),
        "GOPATH": f"{ENVS_ROOT}/go",
        "GOMODCACHE": f"{CACHE_ROOT}/go/mod",
        "GOCACHE": f"{CACHE_ROOT}/go/build",
        "GOPROXY": proxy,
        "GOSUMDB": sumdb,
        "GOTOOLCHAIN": "local",
        # Go locates its standard library from its own executable path, which
        # needs /proc/self/exe. Naming GOROOT removes that dependency, so Go
        # builds at every isolation level, including `basic`. Without it the
        # binary is "trimmed" and answers "cannot find GOROOT directory".
        "GOROOT": install_root,
    }
    return Toolchain(
        name="go",
        environment=environment,
        # Sandbox-installed binaries first, then the image's, so `go install`
        # output shadows the built-in toolchain instead of the other way round.
        path_entries=(f"{ENVS_ROOT}/go/bin", f"{install_root}/bin"),
        reserved_prefixes=("GO",),
    )


def rust_toolchain(
    *,
    registry_url: str | None = None,
    shim_dir: str = DEFAULT_RUST_SHIM_DIR,
    rustup_home: str = DEFAULT_RUSTUP_HOME,
    launcher_dir: str | None = None,
) -> Toolchain:
    """Rust with cargo and rustup.

    ``CARGO_HOME`` holds both a registry cache and installed binaries, so it
    lives in ``/envs`` and the incremental build cache is pointed at
    ``/cache`` separately.
    """

    environment = {
        # Legacy proxies use the image's read-only rustup installation. The
        # procfs-free adapters bypass those proxies, but keep their home for
        # other image tools. Only CARGO_HOME is writable and per-sandbox.
        "CARGO_HOME": f"{ENVS_ROOT}/cargo",
        "RUSTUP_HOME": rustup_home,
        "CARGO_TARGET_DIR": f"{CACHE_ROOT}/cargo-target",
        "CARGO_NET_RETRY": "3",
    }
    if registry_url:
        # Cargo has no single index variable; a mirror is declared as a source
        # replacement for the default registry.
        environment["CARGO_REGISTRIES_CRATES_IO_INDEX"] = registry_url
    return Toolchain(
        name="rust",
        environment=environment,
        # Sandbox-installed binaries, optional adapters, then legacy proxies.
        path_entries=(
            f"{ENVS_ROOT}/cargo/bin",
            *((launcher_dir,) if launcher_dir else ()),
            shim_dir,
        ),
        reserved_prefixes=("CARGO_", "RUSTUP_", "RUSTC_", "RUSTFLAGS"),
        needs_procfs=launcher_dir is None,
    )


def java_toolchain(
    *, maven_repository_url: str | None = None, java_home: str | None = None
) -> Toolchain:
    """JVM languages with Maven and Gradle.

    An image can supply a JDK-shaped ``java_home`` with procfs-free launchers.
    Without one, leave JAVA_HOME to the deployment and use the system JDK.
    """

    environment = {
        "MAVEN_OPTS": f"-Dmaven.repo.local={CACHE_ROOT}/maven",
        "GRADLE_USER_HOME": f"{CACHE_ROOT}/gradle",
        "SBT_OPTS": f"-Dsbt.global.base={ENVS_ROOT}/sbt",
    }
    if maven_repository_url:
        environment["MAVEN_MIRROR_URL"] = maven_repository_url
    if java_home:
        environment["JAVA_HOME"] = java_home
    return Toolchain(
        name="java",
        environment=environment,
        path_entries=(
            f"{ENVS_ROOT}/java/bin",
            *((f"{java_home}/bin",) if java_home else ()),
        ),
        reserved_names=("MAVEN_OPTS", "GRADLE_USER_HOME", "SBT_OPTS", "MAVEN_MIRROR_URL"),
        needs_procfs=java_home is None,
        reserved_prefixes=("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "CLASSPATH"),
    )


_BUILDERS: dict[str, Callable[..., Toolchain]] = {
    "python": python_toolchain,
    "node": node_toolchain,
    "go": go_toolchain,
    "rust": rust_toolchain,
    "java": java_toolchain,
}

SUPPORTED_TOOLCHAINS: tuple[str, ...] = tuple(sorted(_BUILDERS))


def toolchains_needing_procfs(toolchains: Iterable[Toolchain]) -> tuple[str, ...]:
    """Static procfs hints for the configured launch paths.

    Actual availability still requires launching the installed tools.
    """

    return tuple(toolchain.name for toolchain in toolchains if toolchain.needs_procfs)


def toolchains_unavailable_at(
    isolation_features: Iterable[str], toolchains: Iterable[Toolchain]
) -> tuple[str, ...]:
    """Enabled toolchains the given isolation features cannot run.

    Its own function so that the worker's startup warning and the diagnostic it
    publishes agree by construction rather than by two copies of the rule. A
    client that repeats the rule — hard-coding that Rust and the JVM need a
    procfs — will be wrong the next time a language is added.
    """

    if "private_procfs" in set(isolation_features):
        return ()
    return toolchains_needing_procfs(toolchains)


def build_toolchain(name: str, **options: object) -> Toolchain:
    """Build one toolchain by name, ignoring options it does not accept.

    A deployment configures registries for every language it might enable, so
    each builder is handed only the keywords it declares. An unknown name is
    an error rather than a silently empty environment.
    """

    normalized = name.strip().lower()
    builder = _BUILDERS.get(normalized)
    if builder is None:
        supported = ", ".join(SUPPORTED_TOOLCHAINS)
        raise ValueError(f"unknown toolchain {name!r}; supported: {supported}")
    parameters = inspect.signature(builder).parameters
    accepted = {key: value for key, value in options.items() if key in parameters}
    return builder(**accepted)


def compose_environment(
    toolchains: Iterable[Toolchain],
    *,
    base_path: Sequence[str],
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Merge toolchains into one environment and one ``PATH``.

    Two toolchains that set the same variable to different values are a
    configuration error rather than a last-one-wins race, because which one won
    would depend on an ordering no caller declared.
    """

    environment: dict[str, str] = {}
    owners: dict[str, str] = {}
    path_entries: list[str] = []
    for toolchain in toolchains:
        for key, value in toolchain.environment.items():
            previous = owners.get(key)
            if previous is not None and environment[key] != value:
                raise ValueError(
                    f"toolchains {previous!r} and {toolchain.name!r} both define {key}"
                )
            environment[key] = value
            owners[key] = toolchain.name
        for entry in toolchain.path_entries:
            if entry not in path_entries:
                path_entries.append(entry)
    for entry in base_path:
        if entry not in path_entries:
            path_entries.append(entry)
    return environment, tuple(path_entries)


def reserved_environment(
    toolchains: Iterable[Toolchain],
) -> tuple[frozenset[str], tuple[str, ...]]:
    """Collect the names and prefixes no caller may override."""

    names: set[str] = set()
    prefixes: set[str] = set()
    for toolchain in toolchains:
        names.update(name.upper() for name in toolchain.reserved_names)
        names.update(key.upper() for key in toolchain.environment)
        prefixes.update(prefix.upper() for prefix in toolchain.reserved_prefixes)
    return frozenset(names), tuple(sorted(prefixes))


__all__ = [
    "CACHE_ROOT",
    "ENVS_ROOT",
    "SUPPORTED_TOOLCHAINS",
    "Toolchain",
    "build_toolchain",
    "compose_environment",
    "go_toolchain",
    "java_toolchain",
    "node_toolchain",
    "python_toolchain",
    "reserved_environment",
    "rust_toolchain",
    "toolchains_needing_procfs",
    "toolchains_unavailable_at",
]
