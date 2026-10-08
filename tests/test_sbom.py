"""The release SBOM, which has to describe what actually ships.

A supply-chain document is only worth publishing if it is complete: one that
silently omits the database drivers still looks like an SBOM, still validates,
and still gets attached to a release. So these tests do not check that the file
exists — they check that its component set is the one the image installs.

The generator shells out to `uv export --format cyclonedx1.5`, which uv marks
experimental. That is a reason to assert the shape here rather than a reason not
to: if the export changes, this fails instead of publishing a document that
describes the wrong thing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "generate-sbom.sh"

# The drivers the image installs through its extras. An SBOM without these is
# missing the largest attack surface in the project.
ADAPTER_EXTRAS = {"aiomysql", "asyncpg", "pymysql", "redis"}

# Present in the lock, absent from the image: `uv sync --no-dev`.
DEVELOPMENT_ONLY = {"pytest", "pytest-asyncio", "mypy", "ruff"}

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="generating the SBOM needs uv on PATH"
)


@pytest.fixture(scope="module")
def bom(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    outdir = tmp_path_factory.mktemp("sbom")
    completed = subprocess.run(
        [str(SCRIPT), str(outdir)], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((outdir / "agent-sandbox.cdx.json").read_text())


def _components(bom: dict[str, Any]) -> dict[str, str]:
    return {component["name"]: component["version"] for component in bom["components"]}


def _project(name: str) -> dict[str, Any]:
    with open(REPO / name, "rb") as handle:
        return tomllib.load(handle)["project"]


def _development_closure(lock: dict[str, Any]) -> set[str]:
    """Everything a development tool drags in, walked through the lock."""

    packages = {package["name"]: package for package in lock["package"]}
    pending = [name for name in DEVELOPMENT_ONLY if name in packages]
    seen = set(pending)
    while pending:
        for dependency in packages[pending.pop()].get("dependencies", []):
            if dependency["name"] not in seen:
                seen.add(dependency["name"])
                pending.append(dependency["name"])
    return seen


def test_it_is_cyclonedx_and_names_the_application(bom: dict[str, Any]) -> None:
    project = _project("pyproject.toml")
    assert bom["bomFormat"] == "CycloneDX"
    assert bom["specVersion"] == "1.5"

    root = bom["metadata"]["component"]
    assert root["name"] == project["name"]
    assert root["version"] == project["version"]
    assert root["purl"] == f"pkg:pypi/{project['name']}@{project['version']}"
    # `uv` marks its own root node synthetic; a release SBOM names the artifact.
    assert "uv:package:is_project_root" not in json.dumps(root)


def test_every_component_can_be_looked_up(bom: dict[str, Any]) -> None:
    """No purl means no scanner can resolve it.

    The runtime is a path dependency here and the export leaves it without one,
    which is exactly how a component from this repository would drop out of a
    vulnerability report.
    """

    missing = [c["name"] for c in bom["components"] if not c.get("purl")]
    assert not missing, f"components with no purl: {missing}"


def test_the_runtime_is_named_as_the_published_distribution(bom: dict[str, Any]) -> None:
    runtime = _project("runtime/pyproject.toml")
    components = _components(bom)
    assert components[runtime["name"]] == runtime["version"]
    component = next(c for c in bom["components"] if c["name"] == runtime["name"])
    assert component["purl"] == f"pkg:pypi/{runtime['name']}@{runtime['version']}"


def test_it_lists_the_drivers_the_image_installs(bom: dict[str, Any]) -> None:
    """Dropping `--all-extras` would take the database and registry drivers out."""

    components = _components(bom)
    absent = sorted(ADAPTER_EXTRAS - set(components))
    assert not absent, f"the SBOM omits adapter extras the image installs: {absent}"


def test_it_leaves_out_what_the_image_does_not_install(bom: dict[str, Any]) -> None:
    """Dropping `--no-dev` would put pytest and mypy in a released SBOM."""

    present = sorted(DEVELOPMENT_ONLY & set(_components(bom)))
    assert not present, f"the SBOM lists development tools the image does not ship: {present}"


def test_it_describes_the_whole_lock_and_nothing_else(bom: dict[str, Any]) -> None:
    """The component set is the lock minus the development closure.

    Compared against the lock rather than a count, so a package that appears in
    one and not the other is named rather than merely changing a number. The
    development side is a closure, not a list: leaving out pytest also means
    leaving out pluggy, and mypy brings ast-serialize.
    """

    listed = {(c["name"], c["version"]) for c in bom["components"]}
    with open(REPO / "uv.lock", "rb") as handle:
        lock = tomllib.load(handle)
    locked = {(p["name"], p["version"]) for p in lock["package"]}

    # The project itself is the root component, not a listed one.
    project = _project("pyproject.toml")
    locked -= {(project["name"], project["version"])}

    unexpected = sorted(listed - locked)
    assert not unexpected, f"the SBOM lists packages the lock does not: {unexpected}"

    left_out = {name for name, _ in locked - listed}
    assert DEVELOPMENT_ONLY <= left_out, f"the image does not ship {sorted(DEVELOPMENT_ONLY)}"
    closure = _development_closure(lock)
    unexplained = sorted(left_out - closure)
    assert not unexplained, (
        f"the SBOM omits packages that nothing in the development group pulls in: {unexplained}"
    )


def test_it_carries_no_builder_paths(bom: dict[str, Any]) -> None:
    """A published document must not describe the machine that built it."""

    text = json.dumps(bom)
    for marker in ("/Users/", "/home/", str(REPO)):
        assert marker not in text, f"the SBOM leaks a local path: {marker}"
