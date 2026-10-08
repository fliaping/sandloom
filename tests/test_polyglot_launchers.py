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
        executable(home / "bin" / name, 'printf "%s\\n" "$LD_LIBRARY_PATH" "$@"\n')
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
        executable(home / "bin" / name, 'printf "%s\\n" "$@"\n')
    executable(home / "bin" / "cargo", '"$RUSTC" --version\n"$RUSTDOC" --version\n')
    destination = tmp_path / "launchers"
    installer.install_rust(home, destination)
    result = subprocess.run(
        [str(destination / "cargo")], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == ["--sysroot", str(home), "--version"] * 2


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
