#!/usr/bin/env python3
"""Install image-owned Java/Rust launchers that do not require procfs."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
from pathlib import Path


def _launcher(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _library_path(directory: Path) -> str:
    # Export only in the tool process. Mixing JDK/Rust libraries into every
    # sandbox command can change unrelated native programs' linking behavior.
    return f"export LD_LIBRARY_PATH={shlex.quote(str(directory))}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}\n"


def install_java(java_home: Path, destination: Path) -> None:
    """Expose a JDK-shaped JAVA_HOME, including wrappers for every JDK tool."""
    java_home = java_home.resolve(strict=True)
    if not (java_home / "lib" / "libjli.so").is_file():
        raise ValueError(f"JDK has no lib/libjli.so: {java_home}")
    destination.mkdir(parents=True, exist_ok=True)
    for child in java_home.iterdir():
        if child.name != "bin":
            (destination / child.name).symlink_to(child, target_is_directory=child.is_dir())
    for binary in (java_home / "bin").iterdir():
        if binary.is_file() and os.access(binary, os.X_OK):
            _launcher(
                destination / "bin" / binary.name,
                _library_path(java_home / "lib") + f'exec {shlex.quote(str(binary))} "$@"\n',
            )


def install_rust(sysroot: Path, destination: Path) -> None:
    """Call pinned native tools directly; rustup's proxies need self-discovery."""
    sysroot = sysroot.resolve(strict=True)
    for name in ("rustc", "cargo", "rustdoc"):
        binary = sysroot / "bin" / name
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise ValueError(f"Rust tool missing or not executable: {binary}")
        body = _library_path(sysroot / "lib")
        # The procfs-free image pins one compiler, rather than asking rustup to
        # resolve an override or silently downloading another toolchain.
        body += (
            'case "${1-}" in\n'
            "  +*) echo 'This launcher uses the image-pinned Rust toolchain; "
            "build an image with the requested RUST_VERSION.' >&2; exit 2 ;;\n"
            "esac\n"
        )
        if name == "cargo":
            body += (
                f"export RUSTC={shlex.quote(str(destination / 'rustc'))}\n"
                f"export RUSTDOC={shlex.quote(str(destination / 'rustdoc'))}\n"
            )
        else:
            # rustc/rustdoc normally derive the sysroot from current_exe(),
            # which is /proc/self/exe on Linux. Explicit sysroots still work.
            body += (
                "has_sysroot=false\n"
                "for arg do\n"
                '  case "$arg" in --sysroot|--sysroot=*) has_sysroot=true ;; esac\n'
                "done\n"
                f'if [ "$has_sysroot" = false ]; then set -- --sysroot '
                f'{shlex.quote(str(sysroot))} "$@"; fi\n'
            )
        body += f'exec {shlex.quote(str(binary))} "$@"\n'
        _launcher(destination / name, body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path("/usr/local/lib/agent-sandbox"))
    args = parser.parse_args()
    java = shutil.which("java")
    if java is None:
        parser.error("java is not installed")
    java_home = Path(java).resolve(strict=True).parent.parent
    rustc = subprocess.check_output(["rustup", "which", "rustc"], text=True).strip()
    install_java(java_home, args.destination / "java")
    install_rust(Path(rustc).parent.parent, args.destination / "rust" / "bin")


if __name__ == "__main__":
    main()
