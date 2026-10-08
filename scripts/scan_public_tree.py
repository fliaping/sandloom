#!/usr/bin/env python3
"""Scan a directory for anything that must not ship in a public release.

Used two ways:

    # Against this checkout, before anything is built from it.
    ./scripts/scan_public_tree.py .

    # Against an assembled mirror, or a built artifact.
    ./scripts/scan_public_tree.py ../agent-sandbox-public
    ./scripts/scan_public_tree.py dist/agent_sandbox-0.2.0-py3-none-any.whl
    ./scripts/scan_public_tree.py dist/agent_sandbox-0.2.0.tar.gz

Exits 0 when clean, 1 when it finds something, so it can gate a release step.

`--markers-only` skips the secret pass. It exists for scanning something that
legitimately contains high-entropy data — a container filesystem, where the
distribution's keyring armor and CA bundle are high-entropy by nature and would
otherwise bury the marker findings under tens of thousands of lines.

    ./scripts/scan_public_tree.py --markers-only /path/to/exported-image

Two classes of finding, checked for different reasons:

* **Markers** are names. Internal systems, private package sources, and
  colleague-facing service names that describe a topology the public tree is
  not supposed to describe. Matched literally.
* **Secrets** are values. Any high-entropy string that looks like a
  credential. This is deliberately shape-based rather than a list of known bad
  strings: a scanner that hard-codes a leaked token in order to detect that
  token has just leaked it again into a tree this very script guards. To
  check for specific known values, pass them at run time:

      ./scripts/scan_public_tree.py . --deny "$(cat /path/to/secret)"

A file that legitimately needs to contain a marker — a guard test asserting
the marker is absent, say — declares that with a comment:

    # scanner:allow-markers  this file asserts these strings are absent

The allowance covers markers only. Secrets and denied values are still
checked, so an allowance cannot be used to smuggle a credential.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import tarfile
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

# Every marker is assembled from pieces so this file does not match itself.
# tests/test_open_source_contract.py uses the same trick for the same reason.
_MARKERS = (
    "kua" + "ilian",
    "kua" + "ishou",
    "k" + "ws_",
    "k" + "ess",
    "key" + "center",
    "infra_" + "bs_boto3",
    "platform_" + "adapter",
    # Private package sources and internal registries.
    "pypi" + ".corp",
    "npm" + ".corp",
    "registry" + ".corp",
    "mirrors" + ".corp",
    # Internal systems named in the integration guide that was removed from
    # the public tree; keeping them here stops that file from coming back.
    "jan" + "us",
    "ks" + "ap",
    "k" + "log",
    "kwai" + "bi",
)

ALLOWANCE = "scanner:allow-markers"

_SKIP_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".woff",
    ".woff2",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".whl",
    ".so",
    ".pyc",
}
# Tool caches hold recorded test ids and analysis output rather than source, and
# they are the reason a plain `scan_public_tree.py .` on a working checkout used
# to report findings that no release would ever contain.
_SKIP_PARTS = {
    ".git",
    "__pycache__",
    ".venv",
    "node_modules",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    # A coverage database is a SQLite file of recorded test ids and absolute
    # paths, and the HTML report beside it is a copy of the source. Running
    # `--cov` before the release scan is a normal thing to do, and the findings
    # it produced were always about this machine's directory layout rather than
    # about anything a release would contain.
    "htmlcov",
    "coverage.xml",
}
_SKIP_NAMES = (".coverage",)
_SKIP_NAME_PREFIXES = (".coverage.",)

# Long runs of base64/hex-ish characters. 28 is above anything the project
# legitimately emits; the shortest real secret seen here is 32 characters.
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{28,}")
# Things that are high-entropy but not secret: digests, uuids, lockfile pins.
_BENIGN = re.compile(
    r"^[0-9a-f]{32,}$|^[0-9a-f-]{36}$|sha\d*[-:]|integrity|^sha256:|^[0-9a-f]{8}-[0-9a-f]{4}",
    re.I,
)
# A line is skipped for entropy when it is carrying a URL or a checksum rather
# than a value. Without this the scanner drowns in lockfile noise — a registry
# path or a wheel hash reads as high-entropy — and a scanner that cries wolf
# on 1000 lines is one nobody will act on.
_BENIGN_LINE = re.compile(r"://|sha256|sha512|integrity|hash\s*=|wheel\s*=|\.whl|\.tar\.gz", re.I)


def _marker_pattern(marker: str) -> re.Pattern[str]:
    """A marker where it stands alone rather than inside a longer word.

    Three of the markers are short enough to occur inside ordinary third-party
    text: one appears inside a socket option in CPython's `socket.py`, another
    inside a contributor's surname in a comment there, a third inside a
    third-party lockfile. On an image built from this tree alone that is 97
    findings, none of them a leak, and the same words are ordinary English that
    this project's own comments can contain — so the gate would report findings
    nobody can act on, which is how a gate stops being run.

    A boundary is anything that is not a letter or a digit, so `kess://`,
    `pypi.corp.example` and `import klog` still match. Markers that end in
    punctuation are prefixes by construction — `kws_` is written to match
    `kws_bucket` — so they keep their trailing side unanchored.
    """

    trailing = "" if not marker[-1].isalnum() else r"(?![a-z0-9])"
    # Case-insensitive rather than lowered first: scanning an exported image is
    # hundreds of megabytes, and copying all of it to lowercase to search it cost
    # more than the search did.
    return re.compile(rf"(?<![a-z0-9]){re.escape(marker)}{trailing}", re.IGNORECASE)


_MARKER_PATTERNS = tuple((marker, _marker_pattern(marker)) for marker in _MARKERS)

_ENTROPY_THRESHOLD = 4.2


def shannon(text: str) -> float:
    if not text:
        return 0.0
    counts = Counter(text)
    length = len(text)
    return -sum(count / length * math.log2(count / length) for count in counts.values())


def scan(root: Path, deny: list[str], *, markers_only: bool = False) -> list[str]:
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        if _SKIP_PARTS & set(path.parts):
            continue
        if path.name in _SKIP_NAMES or path.name.startswith(_SKIP_NAME_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        relative = path.relative_to(root)

        if ALLOWANCE not in text:
            for marker, pattern in _MARKER_PATTERNS:
                if pattern.search(text):
                    findings.append(f"{relative}: internal marker {marker!r}")

        for value in deny:
            if value and value in text:
                findings.append(f"{relative}: matches a denied value")

        if markers_only:
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            if _BENIGN_LINE.search(line):
                continue
            for candidate in set(_CANDIDATE.findall(line)):
                if _BENIGN.search(candidate):
                    continue
                if shannon(candidate) > _ENTROPY_THRESHOLD:
                    findings.append(
                        f"{relative}:{number}: high-entropy string {candidate[:12]}… "
                        f"({len(candidate)} chars, entropy {shannon(candidate):.2f})"
                    )
    return findings


def _unpack(archive: Path, destination: Path) -> None:
    """Extract a wheel, zip, or source archive so it can be scanned like a tree.

    The checklist names wheels and source archives among the artifacts to
    verify, and a wheel is a zip: pointing this at one used to answer "not a
    directory", which is a check that cannot be run being reported as a check
    that found nothing to look at.
    """

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(destination)
        return
    with tarfile.open(archive) as bundle:
        try:
            bundle.extractall(destination, filter="data")
        except TypeError:  # filter= arrived in 3.12 and was backported
            bundle.extractall(destination)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="directory, wheel, or source archive to scan")
    parser.add_argument(
        "--markers-only",
        action="store_true",
        help="report internal markers and denied values, but not high-entropy "
        "strings; for scanning an artifact that legitimately contains them",
    )
    parser.add_argument(
        "--deny",
        action="append",
        default=[],
        help="exact string that must not appear; repeatable. Read these from "
        "outside the tree so the scanner does not itself carry them.",
    )
    args = parser.parse_args()

    if not args.root.exists():
        print(f"{args.root} does not exist", file=sys.stderr)
        return 2

    if args.root.is_dir():
        return _report(args.root.resolve(), args.deny, markers_only=args.markers_only)

    with tempfile.TemporaryDirectory(prefix="scan-public-tree-") as scratch:
        unpacked = Path(scratch) / "unpacked"
        unpacked.mkdir()
        try:
            _unpack(args.root, unpacked)
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
            print(f"{args.root} is neither a directory nor an archive: {exc}", file=sys.stderr)
            return 2
        return _report(args.root, args.deny, root=unpacked, markers_only=args.markers_only)


def _report(
    display: Path, deny: list[str], *, root: Path | None = None, markers_only: bool = False
) -> int:
    findings = scan((root or display).resolve(), deny, markers_only=markers_only)
    if findings:
        print(f"FAIL: {len(findings)} finding(s) in {display}\n")
        for finding in findings:
            print(f"  {finding}")
        print("\nDo not publish this artifact until every finding is resolved.")
        return 1

    print(f"OK: {display} is clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
