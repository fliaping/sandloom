#!/usr/bin/env python3
"""Verify polyglot compilation at explicit Levels inside the worker image.

Run with the project interpreter as root in a Linux container. No packages or
toolchains are downloaded. The probe and test sandboxes are temporary.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from agent_sandbox.config import Settings
from agent_sandbox.runtime import SandboxRuntime
from agent_sandbox.schemas import ExecRequest

JAVA_BUILD = """
set -eu
jar --create --file app.jar --main-class V V.class
java -jar app.jar
mkdir -p java-project
cd java-project
printf '%s\\n' '<project><modelVersion>4.0.0</modelVersion><groupId>test</groupId><artifactId>basic</artifactId><version>1</version></project>' > pom.xml
mvn --offline validate
echo 'marker 42'
"""

RUST_BUILD = """
set -eu
mkdir -p rust-project/src rust-project/local-lib/src
cd rust-project
cat > Cargo.toml <<'EOF'
[package]
name = "sandbox-check"
version = "0.1.0"
edition = "2021"
[dependencies]
local-lib = { path = "local-lib" }
EOF
cat > build.rs <<'EOF'
fn main() { println!("cargo:rustc-env=SANDBOX_BUILD_MARKER=42"); }
EOF
cat > src/main.rs <<'EOF'
fn main() { println!("marker {}", local_lib::answer()); assert_eq!(env!("SANDBOX_BUILD_MARKER"), "42"); }
#[test]
fn check_answer() { assert_eq!(local_lib::answer(), 42); }
EOF
cat > local-lib/Cargo.toml <<'EOF'
[package]
name = "local-lib"
version = "0.1.0"
edition = "2021"
EOF
cat > local-lib/src/lib.rs <<'EOF'
/// ```
/// assert_eq!(local_lib::answer(), 42);
/// ```
pub fn answer() -> u32 { 42 }
EOF
cargo run --offline -j 2
cargo test --offline -j 2
cargo test --offline --manifest-path local-lib/Cargo.toml -j 2
cargo doc --offline --no-deps -j 2
"""


async def verify_level(level: str, language_checks: dict[str, tuple[str, str]]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="polyglot-check-") as temporary:
        # The manager owns the parent; sandbox UIDs need traversal permission.
        await asyncio.to_thread(Path(temporary).chmod, 0o711)
        settings = Settings(
            internal_token="polyglot-runtime-check",
            isolation_level=level,
            local_root=Path(temporary) / "sandboxes",
            template_root=Path(temporary) / "templates",
            toolchains=["python", "node", "go", "rust", "java"],
            min_free_bytes=0,
            uid_start=59000 + ["basic", "standard", "strict"].index(level),
        )
        await asyncio.to_thread(settings.workspace_root.mkdir)
        runtime = SandboxRuntime(settings)
        try:
            report = await runtime.probe()
            assert report["selected_level"] == level, report
            assert not runtime.sandboxes and not runtime.processes
            assert not list(settings.workspace_root.glob(".probe-*"))
            sandbox = await runtime.create("polyglot-check", 1, settings.uid_start)

            async def execute(name: str, script: str, login: bool = False) -> str:
                result = await runtime.execute(
                    sandbox,
                    ExecRequest(
                        exec_id=name,
                        generation=1,
                        argv=["/bin/sh", "-lc" if login else "-c", script],
                        timeout_seconds=120,
                    ),
                )
                output = result.stdout + result.stderr
                if result.exit_code != 0:
                    raise RuntimeError(f"{level}/{name}: {result.status}: {output[:3000]}")
                return output

            if level == "basic":
                await execute("boundary", "test ! -e /proc/self/exe && test ! -d /proc/1")
            else:
                await execute("boundary", "test -e /proc/self/exe")
            results: dict[str, str] = {}
            for name, (_, script) in language_checks.items():
                if name == "typescript":
                    tools = report["toolchains"]
                    assert isinstance(tools, dict)
                    if tools["checks"]["typescript"]["status"] == "missing":
                        results[name] = "not installed (optional)"
                        continue
                for login in (False, True):
                    output = await execute(f"{name}-{int(login)}", script, login)
                    assert "marker 42" in output, (level, name, output)
                results[name] = "compile/run passed in plain and login shells"
            for login in (False, True):
                assert "marker 42" in await execute(f"java-build-{int(login)}", JAVA_BUILD, login)
                assert "marker 42" in await execute(f"rust-build-{int(login)}", RUST_BUILD, login)
            results["java_build"] = "jar, java -jar and offline Maven validate passed"
            results["rust_build"] = (
                "Cargo local dependency, build.rs, unit/doc tests and docs passed"
            )
            return {"level": level, "toolchains": report["toolchains"], "results": results}
        finally:
            await runtime.shutdown()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--levels",
        nargs="+",
        choices=["basic", "standard", "strict"],
        default=["basic", "standard"],
    )
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() != 0:
        parser.error("run inside a Linux worker container as root")
    path = Path(__file__).with_name("verify-deployment.py")
    spec = importlib.util.spec_from_file_location("deployment_verifier", path)
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    for level in args.levels:
        print(json.dumps(await verify_level(level, verifier.LANGUAGE_CHECKS), indent=2), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
