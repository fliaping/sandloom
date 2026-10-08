"""Every link between the documents, and every heading one points at.

The guides are the only part of this project a reader navigates by hand, and they
are the part nothing compiles: a renamed heading or a moved file leaves a link
that looks fine, reads fine, and lands on a 404 in the rendered repository. The
reference for configuration is the newest and the most cross-linked of them,
which is the sort of document that turns a rename into four broken links.

External URLs are not fetched. A test that needs the network fails on a machine
without it, for a reason that has nothing to do with the change under review.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# `[text](target)` and `[text](target#anchor)`, but not images-only syntax.
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.MULTILINE)
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

_DOCUMENTS = [
    *sorted(ROOT.glob("README*.md")),
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *sorted((ROOT / "docs").glob("*.md")),
]


def _prose(path: Path) -> str:
    """The document without fenced blocks, so a sample URL is not a link."""

    return _FENCE.sub("", path.read_text(encoding="utf-8"))


def _anchors(path: Path) -> set[str]:
    """GitHub's heading anchors, which are the lowercase slug of the heading.

    Punctuation is dropped and spaces become hyphens, so `## Object storage and
    templates` is `#object-storage-and-templates`.
    """

    anchors = set()
    for heading in _HEADING.findall(_prose(path)):
        slug = re.sub(r"[^\w\s-]", "", heading.lower())
        anchors.add(re.sub(r"\s+", "-", slug))
    return anchors


def _relative_links(path: Path) -> list[tuple[str, str]]:
    found = []
    for target in _LINK.findall(_prose(path)):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        file_part, _, anchor = target.partition("#")
        found.append((file_part, anchor))
    return found


def test_every_relative_link_resolves() -> None:
    """A link to a file that is not there, or above the repository root."""

    broken: list[str] = []
    for document in _DOCUMENTS:
        for target, _ in _relative_links(document):
            if not target:
                continue
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{document.relative_to(ROOT)} -> {target}")

    assert not broken, f"these links point at nothing: {broken}"


def test_every_anchor_exists_in_the_document_it_names() -> None:
    """A link to a heading that was renamed, which the reader cannot tell from
    a link to a heading that never existed."""

    broken: list[str] = []
    for document in _DOCUMENTS:
        for target, anchor in _relative_links(document):
            if not anchor:
                continue
            destination = (document.parent / target).resolve() if target else document
            if destination.suffix != ".md" or not destination.exists():
                continue
            if anchor not in _anchors(destination):
                broken.append(f"{document.relative_to(ROOT)} -> {target}#{anchor}")

    assert not broken, f"these anchors name no heading: {broken}"


def test_the_guides_link_to_each_other_rather_than_being_orphans() -> None:
    """A guide no page links to is a guide only its author has read.

    `docs/CONFIGURATION.md` is the reason: it documents every setting, and the
    front page is where a reader looks for it.
    """

    referenced = {target for document in _DOCUMENTS for target, _ in _relative_links(document)}

    orphans = [
        path.relative_to(ROOT).as_posix()
        for path in _DOCUMENTS
        if path.name != "README.md"
        and path.relative_to(ROOT).as_posix() not in referenced
        and path.name not in referenced
    ]

    assert not orphans, f"nothing links to these documents: {orphans}"
