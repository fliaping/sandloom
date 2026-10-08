"""The deployment verifier has to run where the deployment runs.

`scripts/verify-deployment.py` is the one check an operator can point at a
service they did not build — often from a laptop, against a container somewhere
else. That only works while it imports nothing that has to be installed first,
so the promise is pinned here rather than left to whoever adds the next import.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify-deployment.py"


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_the_verifier_imports_only_the_standard_library() -> None:
    """A verifier that needs `pip install` first will not be run."""

    imported = _imported_roots(VERIFIER.read_text(encoding="utf-8"))
    outside = {name for name in imported if name not in sys.stdlib_module_names}

    assert not outside, f"the verifier imports {sorted(outside)}; it must stay stdlib-only"


def test_the_verifier_is_documented_where_a_reader_will_look() -> None:
    """An undocumented check is one nobody runs."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    toolchains = (ROOT / "docs" / "TOOLCHAINS.md").read_text(encoding="utf-8")

    assert "scripts/verify-deployment.py" in readme
    assert "scripts/verify-deployment.py" in toolchains


def test_the_verifier_exposes_the_flags_its_documentation_names() -> None:
    """The docs promise `--strict`; it has to exist."""

    source = VERIFIER.read_text(encoding="utf-8")

    for flag in ("--base-url", "--token", "--strict", "--keep"):
        assert f'"{flag}"' in source, f"{flag} is documented but not implemented"


def _load_verifier() -> Any:
    """Import the script as a module so its checks can be driven directly.

    The `__main__` guard means importing it does not start a verification run.
    """

    spec = importlib.util.spec_from_file_location("verify_deployment", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _OverviewOnlyClient:
    """Answers the scope probe, and records every path it was asked for."""

    def __init__(self, overview: Any, status: int = 200) -> None:
        self.overview = overview
        self.status = status
        self.calls: list[str] = []

    def call(self, method: str, path: str, **_: Any) -> tuple[int, Any]:
        self.calls.append(path)
        if path.endswith("/admin/overview"):
            return self.status, self.overview
        return 500, {"error": "this double only answers the overview"}


class _UnusedSession:
    """Fails loudly if a check runs past the guard and drives a sandbox."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the template checks ran and used session.{name}")


def _scope(module: Any, overview: Any, status: int = 200) -> tuple[bool, int]:
    return module._template_scope(_OverviewOnlyClient(overview, status))


def test_a_fleet_without_an_object_store_skips_the_template_checks() -> None:
    """Templates that stay on their worker are a supported deployment.

    The check builds a template on whichever worker the sandbox landed on and
    then reads the catalog of the node it was pointed at. With more than one
    worker and no store those are different catalogs, so the mismatch reported
    as a failure would send the operator after a bug that the deployment's own
    overview already explains. It is reported as a skip, which `--strict`
    surfaces as a failure for anyone who expected fleet-wide sharing.
    """

    module = _load_verifier()
    client = _OverviewOnlyClient({"templates_shared": False, "worker_total": 3})
    report = module.Report()

    module.check_templates(client, report, session=_UnusedSession())

    assert [row[0] for row in report.rows] == ["skip"], report.rows
    assert client.calls == ["/api/v1/admin/overview"], client.calls
    assert "BLOBSTORE" in report.rows[0][2]


def test_templates_are_still_verified_on_a_single_worker() -> None:
    """One worker means one catalog, so every check can run and must."""

    module = _load_verifier()
    report = module.Report()

    with pytest.raises(AssertionError, match=r"used session\."):
        module.check_templates(
            _OverviewOnlyClient({"templates_shared": False, "worker_total": 1}),
            report,
            session=_UnusedSession(),
        )

    assert report.rows == [], "the check skipped a deployment it should have verified"


def test_a_shared_store_reads_as_fleet_wide() -> None:
    module = _load_verifier()

    assert _scope(module, {"templates_shared": True, "worker_total": 9}) == (True, 9)


def test_an_unreadable_overview_does_not_excuse_the_template_checks() -> None:
    """A store that cannot answer the overview is not a store that cannot share.

    The fallback is the strict case: run the checks and let them report what
    they find, rather than quietly converting a broken deployment into a skip.
    """

    module = _load_verifier()

    assert _scope(module, None) == (True, 1)
    assert _scope(module, {"templates_shared": False, "worker_total": 3}, status=503) == (True, 1)
