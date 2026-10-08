"""Guards the public build against private package sources.

The `scanner:allow-markers` line below exempts this file from marker checks
in scripts/scan_public_tree.py, because it has to name the strings in order to
assert they are absent. The exemption covers markers only; secrets are still
checked here.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_sandbox_runtime.profile import SandboxRuntimeProfile

ROOT = Path(__file__).resolve().parents[1]


def test_public_base_image_contains_portable_sandbox_toolchain() -> None:
    dockerfile = (ROOT / "Dockerfile.base").read_text(encoding="utf-8")

    for contract in (
        "FROM debian:bookworm-slim AS bubblewrap-builder",
        "FROM python:3.13-slim-bookworm",
        "BUBBLEWRAP_VERSION=0.11.2",
        "BUBBLEWRAP_SHA256=69abc30005d2186baf7737feacd8da35633b93cf5af38838ecff17c5f8e924f6",
        "meson setup",
        "libcap2 libseccomp2",
        "git jq",
        "nodejs npm",
        "setpriv prlimit python node npm git rg",
        "test ! -u /usr/bin/bwrap",
    ):
        assert contract in dockerfile

    assert "registry.corp." not in dockerfile
    assert "pypi.corp." not in dockerfile


def test_application_image_uses_public_base_and_unprivileged_bubblewrap() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=agent-sandbox-base:latest" in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    assert "SANDBOX_BUBBLEWRAP_PATH=/usr/bin/bwrap" in dockerfile
    assert 'CMD ["/app/start.sh"]' in dockerfile
    assert "kcsize" not in dockerfile


def test_polyglot_image_extends_the_base_without_replacing_it() -> None:
    """The extra toolchains are opt-in: most deployments do not pay for them."""

    dockerfile = (ROOT / "Dockerfile.polyglot").read_text(encoding="utf-8")

    assert "ARG BASE_IMAGE=agent-sandbox-base:latest" in dockerfile
    assert "FROM ${BASE_IMAGE}" in dockerfile
    # Go, Rust, and a JDK, verified at build time rather than first use.
    assert "command -v go rustc cargo javac java mvn" in dockerfile
    assert "registry.corp." not in dockerfile
    assert "pypi.corp." not in dockerfile


def test_polyglot_image_verifies_the_go_archive_it_downloads() -> None:
    """An unverified tarball from the network would be an unsigned toolchain."""

    dockerfile = (ROOT / "Dockerfile.polyglot").read_text(encoding="utf-8")

    assert "sha256sum -c -" in dockerfile
    # And it fails rather than skipping the check when no digest is available:
    # a missing digest must not degrade into an unverified install.
    assert "exit 1" in dockerfile


def test_polyglot_image_and_the_runtime_agree_on_where_toolchains_live() -> None:
    """The image installs a language and the runtime has to look where it put it.

    These are two files maintained separately, and getting them out of step
    fails in a way that reads like a broken compiler: the image's `rustc` is a
    rustup shim, so a runtime that points RUSTUP_HOME at an empty per-sandbox
    directory turns every Rust build into "could not choose a version of rustc
    to run". The same goes for PATH — the runtime builds it from the toolchain
    definitions, so the image's ENV PATH does not survive and a toolchain that
    lists only /envs hides the compiler the image shipped.

    Binding the constants to the Dockerfile is what makes that class of drift a
    test failure instead of a support ticket.
    """

    from agent_sandbox_runtime import build_toolchain

    dockerfile = (ROOT / "Dockerfile.polyglot").read_text(encoding="utf-8")
    rustup_home = _env_value(dockerfile, "RUSTUP_HOME")
    cargo_home = _env_value(dockerfile, "CARGO_HOME")

    go = build_toolchain("go", proxy="https://goproxy.example")
    rust = build_toolchain("rust", registry_url=None)

    # Rust's shims resolve their toolchains through RUSTUP_HOME, so the runtime
    # must name the image's directory rather than one inside the sandbox.
    assert rust.environment["RUSTUP_HOME"] == rustup_home

    # Both compilers are on PATH at the location the image installed them, and
    # still after the sandbox's own prefix so a local install shadows them.
    assert "/usr/local/go/bin" in go.path_entries
    assert go.path_entries[0] == "/envs/go/bin"
    # rustup puts its shims in the image's CARGO_HOME, not in /usr/local/bin.
    assert f"{cargo_home}/bin" in rust.path_entries
    assert rust.path_entries[0] == "/envs/cargo/bin"

    # The Go archive is unpacked at the directory Go's toolchain expects.
    assert "tar -C /usr/local -xzf" in dockerfile


def _env_value(dockerfile: str, name: str) -> str:
    """Read one value out of the Dockerfile's `ENV A=1 \\` continuation block."""

    for line in dockerfile.splitlines():
        stripped = line.strip().removeprefix("ENV ").lstrip("\\ ").strip()
        if stripped.startswith(f"{name}="):
            # The last assignment in a continuation block ends without a
            # backslash only if the block is well formed; strip either way.
            return stripped.split("=", 1)[1].strip().rstrip("\\").strip()
    raise AssertionError(f"{name} is not set in this Dockerfile")


def test_polyglot_toolchains_live_under_the_read_only_system_mounts() -> None:
    """A sandbox reaches them through /usr, which is already mounted read-only.

    Installing into /opt or /home would need a new mount in every sandbox.
    """

    dockerfile = (ROOT / "Dockerfile.polyglot").read_text(encoding="utf-8")

    assert "/usr/local/go" in dockerfile
    assert "RUSTUP_HOME=/usr/local/rustup" in dockerfile
    assert "CARGO_HOME=/usr/local/cargo" in dockerfile


def test_compose_allows_nested_namespaces_without_privileged_mode() -> None:
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "seccomp=unconfined" in compose
    assert "privileged:" not in compose
    assert "SYS_ADMIN" not in compose
    assert "/var/run/docker.sock" not in compose


def test_the_default_compose_reads_the_env_file_it_tells_you_to_copy() -> None:
    """The quick start says `cp .env.example .env`, so `.env` has to be read.

    Compose uses `.env` for interpolation whether or not a service declares it,
    so the omission is invisible: editing the configuration the documentation
    points at changes nothing, with no error to say so. Only the variables the
    service lists under `environment:` reached the container.
    """

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    configured = [line for line in compose.splitlines() if not line.lstrip().startswith("#")]

    assert any("env_file:" in line for line in configured)
    assert any(line.strip() == "- path: .env" for line in configured)
    # Optional, so a deployment that configures everything through an override
    # is not forced to keep a file it does not use.
    assert any("required: false" in line for line in configured)


def test_the_default_compose_does_not_unmask_proc() -> None:
    """`systempaths=unconfined` is a decision, not a default.

    It unmasks /proc paths for the trusted manager container, which is a wider
    surface than Docker's default profile, and it is what lets the probe reach
    `standard` or `strict`. Both the README and docs/ISOLATION.md describe it as
    something to adopt deliberately, and the quick start's narrative — `auto`
    settles for `basic`, `probe_failures` names why, the override reaches
    `strict` — is only true while the default stays as it is.

    Adding it here to make the JVM and Rust toolchains work would trade a
    documented security posture for convenience without saying so. The toolchain
    warning says so instead.
    """

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    # Comments name the option in order to explain how to opt in, so only the
    # settings that are actually in effect count as setting it.
    configured = [line for line in compose.splitlines() if not line.lstrip().startswith("#")]

    assert not any("systempaths=unconfined" in line for line in configured)
    # And the file has to explain how to opt in, so the omission is not silent.
    assert "docs/ISOLATION.md" in compose


# `| \x60python\x60 | base image | \x60/envs/python-venv\x60, \x60/envs/uv-tools\x60 | \x60/cache/pip\x60 |`
_TOOLCHAIN_ROW = re.compile(
    r"^\|\s*\x60(\w+)\x60\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$", re.MULTILINE
)


def _documented_toolchains() -> dict[str, tuple[list[str], list[str]]]:
    """The `Name | Ships in | Installs to | Caches in` table of docs/TOOLCHAINS.md."""

    text = (ROOT / "docs" / "TOOLCHAINS.md").read_text(encoding="utf-8")
    return {
        name: (
            re.findall(r"\x60(/[^\x60]+)\x60", installs),
            re.findall(r"\x60(/[^\x60]+)\x60", caches),
        )
        for name, _ships, installs, caches in _TOOLCHAIN_ROW.findall(text)
    }


def test_the_documented_toolchain_table_matches_the_runtime() -> None:
    """The table a reader builds a template from, against the definitions.

    `Installs to` is not decoration: it is where a template has to be built so
    that the tree it captures is on the `PATH` a later sandbox composes. A row
    naming a directory the definition never mentions sends a reader to build the
    environment in the wrong place, and the failure shows up much later as a
    missing compiler. `Caches in` states a structural rule instead — caches live
    under `/cache` so they persist with the sandbox and are never shared.
    """

    from agent_sandbox_runtime import SUPPORTED_TOOLCHAINS, build_toolchain

    table = _documented_toolchains()
    assert table, "docs/TOOLCHAINS.md no longer has a toolchain table"

    assert set(table) == set(SUPPORTED_TOOLCHAINS), (
        "the table and the runtime disagree about which languages exist: "
        f"{sorted(set(table) ^ set(SUPPORTED_TOOLCHAINS))}"
    )

    # `build_toolchain` passes each builder only the keywords it declares.
    options: dict[str, object] = {
        "proxy": "https://goproxy.example",
        "registry": "https://npm.example",
        "index_url": "https://pypi.example/simple/",
        "registry_url": None,
        "maven_repository_url": None,
    }

    for name, (installs, caches) in sorted(table.items()):
        toolchain = build_toolchain(name, **options)
        declared = [*toolchain.path_entries, *toolchain.environment.values()]

        for path in installs:
            assert path.startswith("/envs/"), f"{name}: {path} is not under /envs"
            assert any(value.startswith(path) for value in declared), (
                f"docs/TOOLCHAINS.md says {name} installs to {path}, which no path "
                f"entry or environment value of the {name} toolchain names"
            )
        for path in caches:
            assert path.startswith("/cache/"), f"{name}: {path} is not under /cache"

    # The README's example has to be the whole set, or a reader copies a
    # configuration that silently leaves a language out.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = re.search(r"SANDBOX_TOOLCHAINS=([\w,]+)", readme)
    assert example is not None, "README no longer shows SANDBOX_TOOLCHAINS"
    assert set(example.group(1).split(",")) == set(SUPPORTED_TOOLCHAINS)


def _version(text: str) -> tuple[int, ...]:
    """`0.11.10` comes after `0.11.2`, which comparing the strings gets backwards."""

    return tuple(int(part) for part in text.split("."))


def test_the_base_image_pins_a_bubblewrap_the_runtime_will_accept() -> None:
    """Two files that agree today, kept in step by nothing.

    `Dockerfile.base` builds one Bubblewrap version; the runtime refuses to start
    when the binary it finds is below `SandboxRuntimeProfile.minimum_version`.
    Raise that floor without rebuilding the image and every deployment made from
    the image stops at startup — and nothing here fails, because every test in
    this repository runs against the host's bubblewrap, never the image's.
    """

    base = (ROOT / "Dockerfile.base").read_text(encoding="utf-8")
    profile = SandboxRuntimeProfile()

    pinned = _env_value(base, "BUBBLEWRAP_VERSION")
    assert _version(pinned) >= _version(profile.minimum_version), (
        f"Dockerfile.base builds Bubblewrap {pinned}, below the "
        f"{profile.minimum_version} the runtime requires: an image built from it "
        "would refuse to start"
    )

    # So the pin is a pin rather than a comment, and the archive it applies to is
    # built from the same variable: the version and the artifact cannot come
    # apart, and the digest cannot be left behind by an edit to one of them.
    digest = _env_value(base, "BUBBLEWRAP_SHA256")
    assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{digest} is not a sha256 digest"
    assert "sha256sum -c -" in base
    assert (
        "v${BUBBLEWRAP_VERSION}/bubblewrap-${BUBBLEWRAP_VERSION}.tar.xz" in base
    ), "the download no longer derives its version from BUBBLEWRAP_VERSION"

    # The profile hash is what two replicas compare to decide they run the same
    # settings, and it carries the version: bumping one of the two would leave a
    # stricter contract answering to the identity of the older one.
    assert profile.minimum_version in profile.profile_hash, (
        f"profile_hash {profile.profile_hash!r} does not name the required "
        f"Bubblewrap {profile.minimum_version}"
    )


def test_polyglot_image_pins_the_digest_of_the_go_archive_it_downloads() -> None:
    """Behind a mirror, the digest was the step the builder had to go and find.

    `GO_DIST_BASE` exists because go.dev redirects to dl.google.com, which
    corporate and national networks block. Pointing the build at a mirror used to
    mean also producing the official digest by hand from a machine that could
    reach Google — and the network that needs the mirror is the one that cannot.
    The digests of the default version are in the file, keyed by artifact name,
    so this is one build argument rather than two.
    """

    dockerfile = (ROOT / "Dockerfile.polyglot").read_text(encoding="utf-8")
    declared = re.search(r"ARG GO_VERSION=(\S+)", dockerfile)
    assert declared is not None, "Dockerfile.polyglot no longer sets GO_VERSION"
    version = declared.group(1)

    assert 'expected="${GO_SHA256}"' in dockerfile, (
        "an explicit GO_SHA256 no longer takes precedence over the pinned digest"
    )

    for arch in ("amd64", "arm64"):
        artifact = f"go{version}.linux-{arch}.tar.gz"
        digest = re.search(
            rf"{re.escape(artifact)}\)[^=]*expected=\"([0-9a-f]{{64}})\"", dockerfile
        )
        assert digest is not None, (
            f"{artifact} has no pinned digest, so building behind a mirror means "
            "looking one up from the network that blocks the download"
        )

    # For any other version the official index is still consulted, so a bumped
    # GO_VERSION is checked rather than silently trusted.
    assert "https://go.dev/dl/?mode=json&include=all" in dockerfile, (
        "the official release index is no longer consulted for versions this "
        "file does not pin"
    )
