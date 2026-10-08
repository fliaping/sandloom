"""The two distributions this repository publishes have to agree.

Local development resolves `sandloom-runtime` from `runtime/`, but a published
wheel carries only the dependency name and version. Test the release contract:
the two versions move together, the requirement is exact, and each wheel stays
inside its own compatibility import package. Public-index name ownership must
be checked separately before publication.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _project(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)["project"]


# Every `source = { … }` line in the lock, in the shape the lock writes them: one
# line, one brace pair, in a `[[package]]` table.
_SOURCE = re.compile(r"^source = \{ (.*) \}$", re.MULTILINE)

# The only index a published lock may name.
_PUBLIC_INDEX = 'registry = "https://pypi.org/simple"'

APP = _project(ROOT / "pyproject.toml")
RUNTIME = _project(ROOT / "runtime" / "pyproject.toml")


def _runtime_requirement() -> str:
    return next(
        requirement
        for requirement in APP["dependencies"]
        if requirement.startswith(RUNTIME["name"])
    )


def test_the_two_distributions_share_a_version() -> None:
    """They are released together, and a wheel of one against the other is wrong."""

    assert APP["version"] == RUNTIME["version"], (
        "the application and the runtime are separate distributions but one "
        "release; publishing 0.3.0 of one against 0.2.0 of the other installs "
        "a pair nobody tested"
    )


def test_the_runtime_requirement_is_an_exact_pin() -> None:
    """Only the tested application/runtime version pair may resolve."""

    requirement = _runtime_requirement()

    assert f"=={RUNTIME['version']}" in requirement, (
        f"{requirement!r} is not an exact pin; a published wheel would resolve "
        "the name against the index and could install an unrelated distribution"
    )
    assert ">=" not in requirement and "~=" not in requirement and "!=" not in requirement


def test_the_local_source_override_still_exists() -> None:
    """Development must use this checkout's runtime rather than the index."""

    sources = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert sources["tool"]["uv"]["sources"][RUNTIME["name"]] == {
        "path": "runtime",
        "editable": True,
    }


def test_sandloom_names_preserve_compatibility_commands_and_imports() -> None:
    assert APP["name"] == "sandloom"
    assert RUNTIME["name"] == "sandloom-runtime"
    assert APP["scripts"] == {
        "sandloom": "agent_sandbox.main:main",
        "sandloom-doctor": "agent_sandbox.doctor:main",
        "agent-sandbox": "agent_sandbox.main:main",
    }


def test_each_wheel_stays_inside_its_own_package() -> None:
    """Neither distribution may claim the other's import package.

    Both build a top-level `agent_sandbox*` package directory. If the runtime
    ever listed the application's package — or the reverse — the two wheels
    would overwrite each other on install whatever the requirement said.
    """

    app = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = tomllib.loads((ROOT / "runtime" / "pyproject.toml").read_text(encoding="utf-8"))
    app_wheel = app["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    runtime_wheel = runtime["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert app_wheel == ["src/agent_sandbox"]
    assert runtime_wheel == ["src/agent_sandbox_runtime"]


def test_the_console_ships_inside_the_application_package() -> None:
    """The console is generated in Python, so it travels with the package.

    Asserted because a console that lived in a data file outside the package
    would work from the repository and 404 from an installed wheel.
    """

    from agent_sandbox.console import console_html

    page = console_html()

    assert "<!doctype html" in page.lower()
    assert len(page) > 5000, "the console page is a stub; the real one did not ship"
    # No CDN and no build step: the page renders without a network.
    assert "cdn." not in page


def test_every_dependency_in_the_lock_comes_from_a_public_index() -> None:
    """The checklist's "no internal package indexes or private dependencies".

    The lock is what a stranger installs from: `uv sync --frozen` fails outside
    the network for a single entry resolved against an internal registry, in a
    repository that otherwise looks fine — and the hostname is then in a
    published file besides. The accepted shapes are listed rather than the
    forbidden ones, so a source kind nobody has considered yet fails until a
    person has looked at it.
    """

    sources = set(_SOURCE.findall((ROOT / "uv.lock").read_text(encoding="utf-8")))
    assert sources, "uv.lock no longer records where its packages come from"

    for source in sorted(sources):
        if source == _PUBLIC_INDEX:
            continue
        member = re.fullmatch(r'(editable|virtual|directory) = "([^"]*)"', source)
        if member and not member.group(2).startswith("/") and ".." not in member.group(2):
            continue
        raise AssertionError(
            f"uv.lock resolves a package with `source = {{ {source} }}`, which is "
            "neither pypi.org nor a path inside this checkout: the published lock "
            "would send a stranger to a host they may not be able to reach, and "
            "name that host to everyone who reads the file"
        )
