from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def installer() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "install-polyglot-launchers.py"
    spec = importlib.util.spec_from_file_location("polyglot_installer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


# A compiler older than the one that links with lld by default does not know the
# opt-out, and says so with a failing exit. The launcher tests below that are not
# about the linker use tools like that, so the arguments they print are only the
# ones the caller passed.
OLDER_COMPILER = 'case "$*" in *linker-features*) exit 1 ;; esac\n'


def test_java_launcher_preserves_arguments_and_scopes_library_path(
    tmp_path: Path, installer: ModuleType
) -> None:
    home = tmp_path / "JDK with spaces"
    (home / "lib").mkdir(parents=True)
    (home / "lib" / "libjli.so").touch()
    executable(home / "bin" / "java", 'printf "%s\\n" "$LD_LIBRARY_PATH" "$@"\nexit 7\n')
    destination = tmp_path / "java facade"
    installer.install_java(home, destination)
    environment = {**os.environ, "LD_LIBRARY_PATH": "/existing"}
    result = subprocess.run(
        [str(destination / "bin" / "java"), "-jar", "app with spaces.jar", "a; echo injected"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7
    assert result.stdout.splitlines() == [
        f"{home}/lib:/existing",
        "-jar",
        "app with spaces.jar",
        "a; echo injected",
    ]
    assert environment["LD_LIBRARY_PATH"] == "/existing"
    assert (destination / "lib").resolve() == home / "lib"


@pytest.mark.parametrize("arguments", [[], ["--sysroot", "/custom"], ["--sysroot=/custom"]])
def test_rust_launcher_supplies_sysroot_unless_explicit(
    tmp_path: Path, installer: ModuleType, arguments: list[str]
) -> None:
    home = tmp_path / "rust with spaces"
    for name in ("rustc", "cargo", "rustdoc"):
        executable(home / "bin" / name, OLDER_COMPILER + 'printf "%s\\n" "$LD_LIBRARY_PATH" "$@"\n')
    destination = tmp_path / "rust launchers"
    installer.install_rust(home, destination)
    result = subprocess.run(
        [str(destination / "rustc"), *arguments, "input with spaces.rs"],
        env={key: value for key, value in os.environ.items() if key != "LD_LIBRARY_PATH"},
        capture_output=True,
        text=True,
        check=True,
    )
    expected = arguments or ["--sysroot", str(home)]
    assert result.stdout.splitlines() == [str(home / "lib"), *expected, "input with spaces.rs"]


def test_cargo_invokes_adapted_compiler_and_rustdoc(tmp_path: Path, installer: ModuleType) -> None:
    home = tmp_path / "rust"
    for name in ("rustc", "rustdoc"):
        executable(home / "bin" / name, OLDER_COMPILER + 'printf "%s\\n" "$@"\n')
    executable(home / "bin" / "cargo", '"$RUSTC" --version\n"$RUSTDOC" --version\n')
    destination = tmp_path / "launchers"
    installer.install_rust(home, destination)
    result = subprocess.run(
        [str(destination / "cargo")], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == ["--sysroot", str(home), "--version"] * 2


@pytest.mark.parametrize("tool", ["rustc", "rustdoc"])
def test_rust_is_linked_with_the_system_linker_when_the_compiler_can_be_told_to(
    tmp_path: Path, installer: ModuleType, tool: str
) -> None:
    """Rust 1.90's default linker finds itself through /proc/self/exe.

    The `basic` level has no procfs, so the launcher must opt out of it — for
    rustdoc as well, which links every doctest itself. A real compiler would
    refuse an option it does not know; the fake one here accepts everything, so
    the probe finds the opt-out supported."""
    home = tmp_path / "rust"
    for name in ("rustc", "rustdoc", "cargo"):
        executable(home / "bin" / name, 'printf "%s\\n" "$@"\n')
    destination = tmp_path / "launchers"
    installer.install_rust(home, destination)

    result = subprocess.run(
        [str(destination / tool), "input.rs"], capture_output=True, text=True, check=True
    )

    assert result.stdout.splitlines() == [
        "--sysroot",
        str(home),
        "-C",
        "linker-features=-lld",
        "input.rs",
    ]
    cargo = (destination / "cargo").read_text()
    assert "linker-features" not in cargo  # cargo reaches the compiler through RUSTC


def test_rust_override_is_refused_explicitly(tmp_path: Path, installer: ModuleType) -> None:
    home = tmp_path / "rust"
    for name in ("rustc", "rustdoc", "cargo"):
        executable(home / "bin" / name, "echo should-not-run\n")
    destination = tmp_path / "launchers"
    installer.install_rust(home, destination)
    result = subprocess.run(
        [str(destination / "cargo"), "+nightly", "build"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "RUST_VERSION" in result.stderr
    assert "should-not-run" not in result.stdout
