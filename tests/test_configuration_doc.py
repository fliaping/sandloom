"""The configuration reference, and the names the other documents use.

`Settings` reads 73 variables. Before [docs/CONFIGURATION.md] existed, 35 of them
were named by no document at all — the object storage family, the Go and Cargo
mirrors, the UID range, the limits — so the only way to find one was to read the
model. A deployment that cannot discover the name of the variable it needs is not
a deployment this project supports.

A reference like that decays in two directions, and both are checked here:

* a setting added without a row, which is how those 35 went missing;
* a row for a name that nothing reads, which is what a rename leaves behind —
  `Settings` ignores unknown names, so the variable would be documented,
  accepted, and inert.

The last test widens the second direction to every document an operator reads.
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_sandbox.config import Settings

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "CONFIGURATION.md"

# `SANDBOX_FOO` in prose or in a table cell.
_TOKEN = re.compile(r"\bSANDBOX_[A-Z0-9_]+\b")

# Everything a reader is expected to configure from: the front page, the
# contributor guide, the security policy, the sample environment file, and the
# guides. `.env.example` counts because the quick start tells a reader to copy it.
_DOCUMENTS = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "SECURITY.md"]
_DOCUMENTS += sorted((ROOT / "docs").glob("*.md"))
_DOCUMENTS += [ROOT / ".env.example"]

# Read before `Settings` exists, so no field can carry them.
_READ_OUTSIDE_THE_MODEL = {
    "SANDBOX_ENVIRONMENT",
    "SANDBOX_ZONE",
    "SANDBOX_REGION",
}


def _setting_names() -> set[str]:
    return {f"SANDBOX_{name.upper()}" for name in Settings.model_fields}


def _names_in(path: Path) -> set[str]:
    return set(_TOKEN.findall(path.read_text(encoding="utf-8")))


# `| \`SANDBOX_FOO\` | default | meaning |` — the first cell of a table row.
_ROW = re.compile(r"^\|\s*`(SANDBOX_[A-Z0-9_]+)`\s*\|", re.MULTILINE)


def _tabled_names() -> set[str]:
    """The settings the reference gives a row, and therefore a default.

    A name mentioned in passing — in the startup validation list, or inside another
    row's explanation — is not discoverable. `SANDBOX_UID_START` appears in both
    places, so a check that only looked for the string would pass with its row
    deleted.
    """

    return set(_ROW.findall(REFERENCE.read_text(encoding="utf-8")))


def _literals(roots: list[Path]) -> set[str]:
    """`SANDBOX_` names that appear in code under these roots.

    A name is real if the product uses it, and the product uses more of them than
    the settings model defines: `SANDBOX_ENVIRONMENT` is read before settings are
    built, and the error codes a client switches on are string literals — the same
    prefix, a different kind of name. A name that appears nowhere is a typo, which
    is the case this has to catch.
    """

    found: set[str] = set()
    for root in roots:
        for path in root.rglob("*.py"):
            found |= set(_TOKEN.findall(path.read_text(encoding="utf-8")))
    return found


def _in_the_product() -> set[str]:
    return (
        _setting_names()
        | _READ_OUTSIDE_THE_MODEL
        | _literals([ROOT / "src", ROOT / "runtime" / "src", ROOT / "scripts"])
    )


def _in_the_test_suite() -> set[str]:
    return _literals([ROOT / "tests", ROOT / "runtime" / "tests"])


def test_the_reference_names_every_setting() -> None:
    """A setting with no row is a setting nobody can find.

    This is the check that failed for 35 of the 73 when the reference was written,
    and it is the reason the reference exists rather than a sentence here and
    there.
    """

    missing = sorted(_setting_names() - _tabled_names())

    assert not missing, (
        f"{missing} are settings that docs/CONFIGURATION.md gives no row to. A "
        "setting without a row has no default an operator can read"
    )


def test_the_reference_invents_nothing() -> None:
    """A row for a name nothing reads documents a knob that cannot work.

    Only the product counts here: the reference describes the service, so a name
    that exists solely inside the test suite is not a setting and does not belong
    in it.
    """

    invented = sorted(_names_in(REFERENCE) - _in_the_product())

    assert not invented, (
        f"{invented} appear in docs/CONFIGURATION.md but are not settings, error "
        "codes, or anything else the product reads — an operator would set one and "
        "see no effect"
    )


def test_every_document_names_names_that_exist() -> None:
    """The same rule for the README and the guides.

    A typo here costs more than a typo in the reference: the quick start is the
    first thing a reader runs. The test suite's own variables count as real in
    these documents, because docs/INTEGRATION_TESTING.md documents exactly those.
    """

    known = _in_the_product() | _in_the_test_suite()

    unknown: dict[str, list[str]] = {}
    for path in _DOCUMENTS:
        if not path.exists():
            continue
        for name in sorted(_names_in(path) - known):
            unknown.setdefault(name, []).append(str(path.relative_to(ROOT)))

    assert not unknown, (
        "these names appear in documentation but are neither settings, error "
        f"codes, nor anything the code reads, so setting them does nothing: {unknown}"
    )


def test_the_front_page_points_at_the_reference() -> None:
    """A reference nobody links to is a reference nobody finds."""

    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "](docs/CONFIGURATION.md)" in readme
    assert REFERENCE.exists()
