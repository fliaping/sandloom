"""Environment templates verified by running real interpreters from them.

The unit suite proves archives are deterministic and safe. It cannot prove the
thing the feature actually promises: that a Python virtualenv, built once and
then bind-mounted read-only into a different sandbox, still runs. That depends
on absolute paths inside shebangs and `pyvenv.cfg`, on the mount landing at the
path the venv was built at, and on the sandbox UID being able to read a tree it
does not own. All of that needs a real kernel, so these run in a container:

    ./scripts/integration-test.sh sandbox
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_sandbox.config import Settings
from agent_sandbox.runtime import SandboxRuntime
from agent_sandbox.schemas import ExecRequest
from agent_sandbox.templates import LocalTemplateCache, TemplateCatalog, TemplateManager

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Bubblewrap requires Linux"),
    pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap is not installed"),
    pytest.mark.skipif(os.geteuid() != 0, reason="dropping to a sandbox UID requires root"),
]


@pytest.fixture
async def runtime(tmp_path: Path) -> Any:
    local_root = tmp_path / "sandboxes"
    local_root.mkdir()
    # Every ancestor must be traversable by the sandbox UID; see the isolation
    # suite for why pytest's 0700 tmp_path base is the exception, not the rule.
    for ancestor in (local_root, *local_root.parents):
        os.chmod(ancestor, os.stat(ancestor).st_mode | 0o011)
        if ancestor == Path(tempfile.gettempdir()):
            break

    settings = Settings(
        internal_token="integration-token",
        local_root=local_root,
        database_url=f"sqlite+aiosqlite:///{tmp_path}/control.db",
        isolation_level="auto",
        network_mode="host",
        uid_start=20000,
        uid_end=20099,
        template_root=tmp_path / "templates",
    )
    instance = SandboxRuntime(settings)
    await instance.probe()
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _run(runtime: SandboxRuntime, sandbox: Any, script: str, **overrides: Any) -> Any:
    options: dict[str, Any] = {
        "exec_id": f"exec-{os.urandom(4).hex()}",
        "generation": sandbox.generation,
        "argv": ["/bin/bash", "-c", script],
        "timeout_seconds": 120,
    }
    options.update(overrides)
    return await runtime.execute(sandbox, ExecRequest(**options))


def _build_venv(destination: Path) -> None:
    """Create a real virtualenv with a package importable only from it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "venv", str(destination)], check=True, capture_output=True
    )
    site_packages = next(destination.glob("lib/python*/site-packages"))
    (site_packages / "template_marker.py").write_text("VALUE = 'from-template'\n")


async def test_a_venv_from_a_template_actually_runs(runtime: SandboxRuntime) -> None:
    """The core promise: build an environment once, run it in a fresh sandbox.

    The venv is built at /envs/python-ml inside the *builder* sandbox and
    mounted at the same path in the consumer, because a venv's shebangs and
    pyvenv.cfg record absolute paths.
    """
    builder = await runtime.create("sandbox-builder", 1, 20001)
    _build_venv(builder.root / "envs" / "python-ml")

    record = runtime.build_template(
        builder, name="python-ml", source_path="/envs/python-ml", description="venv"
    )

    consumer = await runtime.create("sandbox-consumer", 1, 20002)
    runtime.attach_templates(consumer.sandbox_id, [record])
    result = await _run(
        runtime,
        consumer,
        "/envs/python-ml/bin/python -c 'import template_marker; print(template_marker.VALUE)'",
    )

    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == "from-template"


async def test_a_template_is_read_only_inside_the_sandbox(runtime: SandboxRuntime) -> None:
    """Shared templates must be immutable, or one tenant can poison another."""
    builder = await runtime.create("sandbox-ro-builder", 1, 20003)
    _build_venv(builder.root / "envs" / "shared")
    record = runtime.build_template(builder, name="shared", source_path="/envs/shared")

    consumer = await runtime.create("sandbox-ro-consumer", 1, 20004)
    runtime.attach_templates(consumer.sandbox_id, [record])
    result = await _run(runtime, consumer, "touch /envs/shared/injected 2>&1 || echo blocked")

    assert "blocked" in result.stdout or result.exit_code != 0
    assert not (runtime.templates.cache.path_for("shared", record.digest) / "injected").exists()


async def test_two_sandboxes_share_one_materialized_copy(runtime: SandboxRuntime) -> None:
    """Sharing the tree is what makes density cheap; a per-sandbox copy is not."""
    builder = await runtime.create("sandbox-share-builder", 1, 20005)
    _build_venv(builder.root / "envs" / "common")
    record = runtime.build_template(builder, name="common", source_path="/envs/common")

    first = await runtime.create("sandbox-share-a", 1, 20006)
    second = await runtime.create("sandbox-share-b", 1, 20007)
    runtime.attach_templates(first.sandbox_id, [record])
    runtime.attach_templates(second.sandbox_id, [record])

    for sandbox in (first, second):
        result = await _run(
            runtime, sandbox, "/envs/common/bin/python -c 'import template_marker; print(1)'"
        )
        assert result.exit_code == 0, result.stderr

    revisions = runtime.templates.cache.list_revisions()
    assert len([item for item in revisions if item[0] == "common"]) == 1


async def test_two_environments_are_mounted_side_by_side(runtime: SandboxRuntime) -> None:
    """Applications need more than one: a Python env and a Node tree together.

    Each template mounts at `/envs/<name>`, so mounting several is several bind
    mounts into one sandbox. That is the case a single-template test cannot
    reach, and the one where a mount target that collided, or a mount list that
    stopped after the first entry, would look like a missing environment.
    """
    builder = await runtime.create("sandbox-multi-builder", 1, 20010)
    _build_venv(builder.root / "envs" / "python-env")
    (builder.root / "envs" / "node-env" / "bin").mkdir(parents=True)
    (builder.root / "envs" / "node-env" / "bin" / "hello.js").write_text(
        "console.log('from-node-env')\n"
    )
    python_record = runtime.build_template(
        builder, name="python-env", source_path="/envs/python-env"
    )
    node_record = runtime.build_template(builder, name="node-env", source_path="/envs/node-env")

    consumer = await runtime.create("sandbox-multi-consumer", 1, 20011)
    runtime.attach_templates(consumer.sandbox_id, [python_record, node_record])
    result = await _run(
        runtime,
        consumer,
        "/envs/python-env/bin/python -c 'import template_marker; print(template_marker.VALUE)'"
        " && cat /envs/node-env/bin/hello.js"
        " && ls /envs",
    )

    assert result.exit_code == 0, result.stderr
    assert "from-template" in result.stdout
    assert "from-node-env" in result.stdout
    # Both are mounted under /envs, side by side, and neither replaced the other.
    assert result.stdout.split()[-2:] == ["node-env", "python-env"]


async def test_an_unattached_sandbox_does_not_see_the_template(runtime: SandboxRuntime) -> None:
    """Templates are opt-in, so an unrelated sandbox must not inherit them."""
    builder = await runtime.create("sandbox-optin-builder", 1, 20008)
    _build_venv(builder.root / "envs" / "private")
    runtime.build_template(builder, name="private", source_path="/envs/private")

    other = await runtime.create("sandbox-optin-other", 1, 20009)
    result = await _run(runtime, other, "ls /envs")

    assert "private" not in result.stdout


async def test_a_rebuilt_identical_environment_reuses_the_digest(
    runtime: SandboxRuntime,
) -> None:
    """Determinism against a real venv, not a synthetic tree."""
    builder = await runtime.create("sandbox-digest", 1, 20010)
    _build_venv(builder.root / "envs" / "first")
    shutil.copytree(
        builder.root / "envs" / "first",
        builder.root / "envs" / "second",
        symlinks=True,
    )

    first = runtime.build_template(builder, name="first", source_path="/envs/first")
    second = runtime.build_template(builder, name="second", source_path="/envs/second")

    assert first.digest == second.digest


async def test_a_template_survives_a_worker_restart(
    runtime: SandboxRuntime, tmp_path: Path
) -> None:
    """A published template must outlive the process that built it."""
    builder = await runtime.create("sandbox-restart", 1, 20011)
    _build_venv(builder.root / "envs" / "persisted")
    record = runtime.build_template(builder, name="persisted", source_path="/envs/persisted")
    catalog_path = tmp_path / "templates" / "catalog.json"
    TemplateCatalog(catalog_path).publish(record)

    # A new manager over the same disk, as a restarted worker would have.
    reopened = TemplateManager(LocalTemplateCache(tmp_path / "templates"))
    resolved = TemplateCatalog(catalog_path).get("persisted")

    assert resolved is not None
    assert reopened.materialize(resolved).is_dir()


async def test_building_from_an_illegal_path_is_rejected(runtime: SandboxRuntime) -> None:
    """/home and /cache hold credentials and must never become a shared template."""
    builder = await runtime.create("sandbox-illegal", 1, 20012)

    with pytest.raises(ValueError, match="template source path"):
        runtime.build_template(builder, name="creds", source_path="/home/sandbox")

    with pytest.raises(ValueError, match="template source path"):
        runtime.build_template(builder, name="escape", source_path="/envs/../../etc")
