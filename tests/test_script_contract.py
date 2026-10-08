"""The commands the documentation tells a reader to run.

A documented command that fails is worse than no documentation: the reader
copies it, gets `permission denied`, and concludes the project does not work.
That happened — `verify-deployment.py` was committed 0644 while two documents
tell a reader to execute it directly, and CI did not notice because it runs the
file through `uv run python`.

The same applies to a command that is subtly not the one CI uses, and to a
document that enumerates something the code decides: both drift quietly, and
neither is covered by running the code.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest


def test_image_scan_removes_only_its_own_container() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "scan-image.sh").read_text()
    assert 'SCAN_CONTAINER=$(docker create "${IMAGE}")' in script
    assert 'docker rm -f -v "${SCAN_CONTAINER}"' in script
    assert "docker create --name" not in script
    assert "agent-sandbox-image-scan" not in script

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

# `./scripts/name.sh` in prose or in a fenced block. The leading `./` is what
# distinguishes a command a reader runs from a path mentioned in passing.
_INVOKED = re.compile(r"\./scripts/([A-Za-z0-9_.-]+)")

_DOCUMENTATION = [*sorted(ROOT.glob("README*.md")), ROOT / "CONTRIBUTING.md"]
_DOCUMENTATION += sorted((ROOT / "docs").glob("*.md"))

# `  middleware)` inside the `case` block.
_MODE = re.compile(r"^  ([a-z]+)\)$", re.MULTILINE)
_USAGE = re.compile(r"usage: \$0 \[([a-z|]+)\]")


def _documented_invocations() -> dict[str, list[str]]:
    """Which script each document tells a reader to run."""

    found: dict[str, list[str]] = {}
    for path in _DOCUMENTATION:
        for name in _INVOKED.findall(path.read_text(encoding="utf-8")):
            found.setdefault(name, []).append(path.name)
    return found


def _modes_in(path: Path) -> set[str]:
    """The runner modes a file documents.

    A bare `./scripts/integration-test.sh` line documents the default, which is
    the `all` branch; the others name their mode.
    """

    found = re.findall(
        r"\./scripts/integration-test\.sh(?: +([a-z]+))?", path.read_text(encoding="utf-8")
    )
    return {mode or "all" for mode in found}


@pytest.mark.skipif(sys.platform == "win32", reason="executable bits are a POSIX property")
def test_every_script_the_docs_invoke_can_be_invoked() -> None:
    """`./scripts/x` has to be executable, or the reader's first command fails.

    It did: `verify-deployment.py` was committed 0644 while README.md and
    docs/TOOLCHAINS.md both tell a reader to run it directly. CI did not notice
    because it runs the file through `uv run python`.
    """

    invocations = _documented_invocations()
    assert invocations, "no documentation invokes a script, so this proves nothing"

    for name, documents in sorted(invocations.items()):
        path = SCRIPTS / name
        assert path.exists(), f"{documents} invoke scripts/{name}, which does not exist"
        mode = path.stat().st_mode
        assert mode & stat.S_IXUSR, (
            f"scripts/{name} is not executable, but {documents} tell a reader to run it"
        )


def test_every_script_is_named_somewhere() -> None:
    """A tool nobody documents is a tool nobody runs, and then nobody maintains.

    `generate-sbom.sh` is the reason this exists: it is the only thing that
    produces a release artifact, and it was added without being referenced by
    any document or workflow.
    """

    referenced = "\n".join(
        [path.read_text(encoding="utf-8") for path in _DOCUMENTATION]
        + [
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
        ]
    )
    orphans = [
        path.name
        for path in sorted(SCRIPTS.iterdir())
        if path.is_file() and path.name not in referenced
    ]
    assert not orphans, f"nothing references these scripts: {orphans}"


@pytest.mark.parametrize("name", ["generate-sbom.sh", "verify-distributions.sh"])
def test_release_metadata_uses_the_project_interpreter(name: str) -> None:
    """An old system Python must not break release scripts after tests pass."""
    source = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "import tomllib" in source
    assert "uv run --frozen --no-sync python -" in source
    assert not re.search(r"(?:^|\$\()python3\s+-", source, flags=re.MULTILINE)


def test_the_runner_documents_exactly_the_modes_it_implements() -> None:
    """A usage line listing a mode that does not exist wastes a reader's time.

    It is the same failure in the other direction, so all of them are checked:
    what the script accepts, what its usage line advertises, what its header
    comment tells a reader to type, and what the guide that enumerates the modes
    says — that last one went stale the moment `polyglot` was added.
    """

    source = (SCRIPTS / "integration-test.sh").read_text(encoding="utf-8")

    # Everything from the first case branch to the last one, so the pattern does
    # not pick up unrelated indented lines ending in a parenthesis.
    body = source[source.index('case "${1:-all}" in') : source.index("esac")]
    implemented = set(_MODE.findall(body))

    usage = _USAGE.search(source)
    assert usage is not None, "integration-test.sh has no usage line"
    advertised = set(usage.group(1).split("|"))

    assert implemented == advertised, (
        f"the usage line and the case block disagree: {sorted(advertised ^ implemented)}"
    )

    header = _modes_in(SCRIPTS / "integration-test.sh")
    assert header == implemented, (
        f"the header comment and the case block disagree: {sorted(header ^ implemented)}"
    )

    guide = _modes_in(ROOT / "docs" / "INTEGRATION_TESTING.md")
    assert guide == implemented, (
        f"docs/INTEGRATION_TESTING.md and the case block disagree: {sorted(guide ^ implemented)}"
    )
    assert os.access(SCRIPTS / "integration-test.sh", os.X_OK)


CI_COMMANDS = (
    "uv run pytest -q",
    "uv run pytest -q runtime/tests",
    "uv run ruff check src runtime/src tests runtime/tests examples scripts",
    "uv run mypy src runtime/src scripts examples",
)

# The release checks `docs/OPEN_SOURCE_RELEASE.md` tells a releaser to run. Each is
# named by its path or its command rather than by a job step name, so that a job
# renaming a step does not silently satisfy this.
RELEASE_GATES = (
    "scripts/scan_public_tree.py .",
    "scripts/scan-image.sh",
    "scripts/verify-distributions.sh",
    "scripts/integration-test.sh polyglot",
    "scripts/verify-fleet.py --strict",
    "uvx pip-audit --no-deps --disable-pip --vulnerability-service osv",
)


def _flatten(path: Path) -> str:
    """One line, so a command split across a shell continuation still matches."""

    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8").replace("\\\n", " "))


def _workflows() -> str:
    """Every workflow in the repository, flattened into one line.

    Not just `ci.yml`: the polyglot image has its own workflow, because it takes
    minutes to build and is path-filtered rather than run on every pull request.
    Reading one file would call that gate unwired, and the correction that
    suggests is a second copy of the job -- which is how the same gate came to
    run in both files at once.
    """

    return " ".join(
        _flatten(path) for path in sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    )


def _invoked_scripts(workflow: str) -> set[str]:
    """Every `./scripts/<name>` a workflow runs, whatever arguments it passes."""

    return set(re.findall(r"\./scripts/([A-Za-z0-9_.-]+)", workflow))


def test_the_release_gates_run_in_ci_and_are_the_ones_the_checklist_names() -> None:
    """A gate the checklist names and no job runs is a gate nobody runs.

    All four of these were in that position: the mirror script, the tree scan, the
    distribution verification and the dependency audit were each documented as a
    release step and executed by nothing but a human reading the document — the
    same "documented but inert" shape that `generate-sbom.sh` had before a
    workflow referenced it. The reverse is checked too, so a job cannot come to
    run a gate the checklist no longer asks for.
    """

    workflow = _workflows()
    checklist = _flatten(ROOT / "docs" / "OPEN_SOURCE_RELEASE.md")

    for gate in RELEASE_GATES:
        assert gate in checklist, (
            f"docs/OPEN_SOURCE_RELEASE.md no longer asks a releaser to run {gate}; "
            "either restore it or drop it from this list"
        )
        assert gate in workflow, (
            f"{gate} is a release gate the checklist tells a releaser to run and no "
            "CI job runs it, so nothing notices when it stops working"
        )

    # And the other direction, which the paragraph above promises: a script CI
    # runs without the checklist naming it is a gate nobody releasing this
    # project knows to wait for.
    undocumented = sorted(
        name for name in _invoked_scripts(workflow) if f"scripts/{name}" not in checklist
    )
    assert not undocumented, (
        "CI runs scripts the release checklist never names: "
        f"{['scripts/' + name for name in undocumented]}"
    )


def test_the_documented_example_command_reaches_the_documented_port() -> None:
    """`python examples/quickstart.py` has to work with no arguments at all.

    The README's command carries no `--base-url` and the deployment verifier
    passed one on every run, so the default a reader depends on was never
    exercised: a default that drifted, or a Compose file that stopped publishing
    that port, would fail for every reader while CI stayed green. One of the two
    example runs now uses the documented form; this pins the three places the
    address is written down to each other.
    """

    default = re.compile(r'SANDBOX_BASE_URL",\s*"([^"]+)"')
    written = {
        name: default.search((ROOT / name).read_text(encoding="utf-8"))
        for name in (
            "examples/quickstart.py",
            "examples/templates.py",
            "scripts/verify-deployment.py",
        )
    }
    missing = [name for name, match in written.items() if match is None]
    assert not missing, f"these no longer take an address from SANDBOX_BASE_URL: {missing}"
    addresses = {match.group(1) for match in written.values() if match is not None}
    assert len(addresses) == 1, f"the examples disagree about where the service is: {addresses}"

    address = addresses.pop()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "python examples/quickstart.py" in readme, (
        "the README no longer gives the no-argument command this is about"
    )
    assert address in readme or "8080" in readme, (
        f"the README no longer names the address the examples default to: {address}"
    )

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "${SANDBOX_HTTP_PORT:-8080}:8080" in compose
    assert "${SANDBOX_BIND_ADDRESS:-127.0.0.1}" in compose
    assert address.endswith(":8080"), "the examples must match the default published port"


# Every document that tells a reader how to check their change locally.
_GUIDES = ["CONTRIBUTING.md", "README.md"]


def test_the_guides_run_what_ci_runs() -> None:
    """A guide whose commands drifted from CI's reviews a different project.

    Naming a command without the arguments it needs is the same defect one step
    milder: `ruff check src` passes on a change to `examples/`, which CI then
    rejects, and a `pytest` that never collects `runtime/tests` reports on half the
    repository. The README carried both of those while CONTRIBUTING.md carried
    neither, so both files are checked.
    """

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    missing_from_ci = [command for command in CI_COMMANDS if command not in workflow]
    assert not missing_from_ci, f"CI no longer runs: {missing_from_ci}"

    for name in _GUIDES:
        guide = (ROOT / name).read_text(encoding="utf-8")
        missing = [command for command in CI_COMMANDS if command not in guide]
        assert not missing, f"{name} does not give the command CI uses: {missing}"


def test_a_guide_never_offers_a_runner_mode_that_does_not_exist() -> None:
    """A second document enumerating the modes is a second chance to be wrong.

    The guide that owns the list is checked for completeness; every other document
    is checked for the other failure, a mode a reader types and the script rejects.
    It found one: the README called a bare `./scripts/integration-test.sh`
    "middleware + isolation", which had stopped being true when `polyglot` was
    added to the default.
    """

    source = (SCRIPTS / "integration-test.sh").read_text(encoding="utf-8")
    body = source[source.index('case "${1:-all}" in') : source.index("esac")]
    implemented = set(_MODE.findall(body))

    for name in _GUIDES:
        undocumented = _modes_in(ROOT / name) - implemented
        assert not undocumented, (
            f"{name} tells a reader to run {sorted(undocumented)}, which "
            "integration-test.sh does not implement"
        )


# `${array[@]}` — the form whose bracket content, if any, follows `[@]`.
_ARRAY_EXPANSION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\[@\]([^}]*)\}")


def test_no_shell_script_expands_a_possibly_empty_array_unguarded() -> None:
    """`set -u` and an empty array do not mix, and macOS ships bash 3.2.

    `${a[@]+"${a[@]}"}` is the portable form — the `+` makes the expansion
    conditional, which bash 3.2 needs and bash 5 tolerates without noticing.
    `build-public-mirror.sh` shipped the unguarded form on the exact path its own
    documented invocation takes, the array being empty unless `--deny-file` is
    passed, so the script that produces the publishable artifact died with
    "DENY_ARGS[@]: unbound variable" on macOS after exporting the tree and before
    committing it. `integration-test.sh` had the same bug in the same week.
    """

    offenders: list[str] = []
    for path in [*sorted(SCRIPTS.glob("*.sh")), ROOT / "start.sh"]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if "set -u" not in text:
            continue
        # Whole-line comments are dropped first: the comment explaining this very
        # rule quotes the unguarded form, and a guard that reads its own
        # documentation as code would fail on the fix it describes.
        code = "".join(
            line for line in text.splitlines(keepends=True) if not line.lstrip().startswith("#")
        )
        offenders += [
            f"{path.name}: {match.group(0)}"
            for match in _ARRAY_EXPANSION.finditer(code)
            if not match.group(2)
        ]

    assert not offenders, (
        f"these array expansions are not guarded against an empty array under "
        f'`set -u`: {offenders}. Write ${{name[@]+"${{name[@]}}"}} instead'
    )


def test_the_mirror_script_implements_the_flags_its_usage_lines_show() -> None:
    """The flag that decides whether a publishable mirror is usable at all.

    `--verify` exists because the working tree and the commit are not the same
    thing: the suites passed locally with a fix that had never been committed, and
    the exported tree failed. A flag shown in the header and not implemented is
    worse than an absent one, because a releaser would believe the mirror had been
    verified.
    """

    source = (SCRIPTS / "build-public-mirror.sh").read_text(encoding="utf-8")
    documented = {
        flag
        for line in source.splitlines()
        if line.startswith("#   ./scripts/build-public-mirror.sh")
        for flag in re.findall(r"--[a-z][a-z-]*", line)
    }
    options = source[source.index("while [[ $# -gt 0 ]]") : source.index("esac")]
    handled = set(re.findall(r"^    (--[a-z][a-z-]*)\)$", options, re.MULTILINE))

    assert documented == handled, (
        f"documented but not handled: {sorted(documented - handled)}; handled but "
        f"documented nowhere: {sorted(handled - documented)}"
    )


def test_the_load_test_refuses_a_container_it_cannot_sample(tmp_path: Path) -> None:
    """`--container` naming nothing used to be a silent no-op.

    The flag exists to collect memory and PID counts. A wrong name printed no
    warning, exited 0, and produced a report missing exactly the numbers it was
    asked for -- and a wrong name is the common case, because the container is
    named after the compose project, which is the directory. `docker` is stubbed
    to fail here so the assertion does not depend on a daemon or a deployment.
    """

    stub = tmp_path / "bin"
    stub.mkdir()
    docker = stub / "docker"
    docker.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "load-test.py"),
            "--token",
            "t",
            "--container",
            "agent-sandbox-nothing-by-this-name-1",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{stub}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=60,
    )

    assert result.returncode == 2, result.stdout + result.stderr
    assert "agent-sandbox-nothing-by-this-name-1" in result.stderr
    assert "docker compose ps" in result.stderr


_FENCED = re.compile(r"```[^\n]*\n(.*?)```", re.S)


def test_a_documented_load_test_looks_its_container_up_rather_than_naming_one() -> None:
    """The name belongs to the reader's directory, not to whoever wrote the doc.

    SIZING.md used to spell out `agent-sandbox-oss-agent-sandbox-1`, which is the
    name the author's checkout produced; anyone whose directory is named
    differently copies a command that samples nothing.
    """

    found = 0
    for path in _DOCUMENTATION:
        for block in _FENCED.findall(path.read_text(encoding="utf-8")):
            joined = block.replace("\\\n", " ")
            if "--container" not in joined:
                continue
            found += 1
            assert '"$(docker compose ps' in joined, (
                f"{path.name} writes a container name out instead of asking compose for "
                f"it: {joined.strip()}"
            )

    assert found, "no documented --container command was found, so nothing was checked"
