"""The public tree must not carry internal markers.

The `scanner:allow-markers` line below exempts this file from marker checks
in scripts/scan_public_tree.py, because it has to name the strings in order to
assert they are absent. The exemption covers markers only; secrets are still
checked here.
"""

from __future__ import annotations

import importlib.util
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scripts" / "scan_public_tree.py"
_BINARY_ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".pdf"}


def _is_public_text(path: Path) -> bool:
    return (
        path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix.lower() not in _BINARY_ASSET_SUFFIXES | {".pyc"}
    )


def _public_text_files() -> list[Path]:
    roots = [
        ROOT / "src",
        ROOT / "runtime/src",
        ROOT / "tests",
        ROOT / "runtime/tests",
        ROOT / "docs",
        ROOT / "deploy",
    ]
    files = [
        ROOT / "README.md",
        ROOT / "pyproject.toml",
        ROOT / ".env.example",
        ROOT / "Dockerfile",
        ROOT / "Dockerfile.base",
    ]
    for root in roots:
        files.extend(
            path
            for path in root.rglob("*")
            if _is_public_text(path)
        )
    return files


def test_binary_illustrations_are_not_decoded_as_source(tmp_path: Path) -> None:
    image = tmp_path / "architecture.PNG"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + ("k" + "ess").encode())
    source = tmp_path / "architecture.md"
    source.write_text("# Architecture\n", encoding="utf-8")

    assert not _is_public_text(image)
    assert _is_public_text(source)


def test_public_build_metadata_has_no_private_package_sources() -> None:
    public_files = [
        ROOT / "pyproject.toml",
        ROOT / "Dockerfile",
        ROOT / "Dockerfile.base",
        ROOT / ".env.example",
    ]
    content = "\n".join(path.read_text(encoding="utf-8") for path in public_files)

    assert "pypi.corp" not in content
    assert "npm.corp" not in content
    assert "registry.corp" not in content
    assert "infra-framework" not in content


def test_startup_script_never_contains_a_default_bearer_token() -> None:
    script = (ROOT / "start.sh").read_text(encoding="utf-8")

    assert not re.search(r"SANDBOX_INTERNAL_TOKEN:-[^}]", script)
    assert "SANDBOX_INTERNAL_TOKEN is required" in script


def test_public_tree_contains_no_enterprise_adapter_markers() -> None:
    # Construct markers so this guard does not match its own source text.
    forbidden = (
        "kua" + "ilian",
        "kua" + "ishou",
        "k" + "ws_",
        "k" + "ess",
        "key" + "center",
        "infra_" + "bs_boto3",
        "platform_" + "adapter",
    )
    findings: list[str] = []
    for path in _public_text_files():
        content = path.read_text(encoding="utf-8").lower()
        for marker in forbidden:
            if marker in content:
                findings.append(f"{path.relative_to(ROOT)}: {marker}")

    assert findings == []


def _scan(monkeypatch: pytest.MonkeyPatch, target: Path, *extra: str) -> int:
    """Run the release scanner the way the checklist invokes it."""

    spec = importlib.util.spec_from_file_location("scan_public_tree", SCANNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("sys.argv", ["scan_public_tree.py", str(target), *extra])
    return int(module.main())


def _package(tmp_path: Path, body: dict[str, str]) -> Path:
    source = tmp_path / "src"
    for name, text in body.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return source


# Markers as they are written internally, assembled here so this guard does not
# match its own source text. The scanner's own copy is assembled the same way.
_STANDALONE = "k" + "log"
_PREFIX = "k" + "ws_"


def test_a_marker_is_found_where_it_stands_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the marker check, and the half that must not be relaxed.

    A configuration file naming the internal collector is exactly the leak this
    gate exists for; the boundary rule only excuses the marker when it is part of
    a longer word.
    """

    planted = _package(tmp_path, {"conf.yaml": f"endpoint: {_STANDALONE}://collector.internal\n"})

    assert _scan(monkeypatch, planted) == 1


def test_a_prefix_marker_still_matches_inside_an_identifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker written as a prefix has to keep matching inside an identifier.

    Anchoring the trailing side of every marker would break the ones that end in
    punctuation, which are the ones whose whole purpose is to match a prefix.
    """

    planted = _package(tmp_path, {"backup.sh": f"bucket=s3://{_PREFIX}bucket/data\n"})

    assert _scan(monkeypatch, planted) == 1


def test_a_word_that_merely_contains_a_marker_is_not_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the gate reports findings nobody can act on.

    Each of these occurs in third-party code inside this project's own image, and
    the first is an ordinary English word this project's own comments contain.
    A release gate that fires on them is one that gets ignored.
    """

    # Assembled like the markers are: this file is itself scanned, by substring,
    # by the guard above.
    words = _package(
        tmp_path,
        {
            "comments.txt": (
                "The back" + "log drains when the client catches up.\n"
                "Thanks to Magnus Kes" + "sler for the correction.\n"
                "class WebpackLogger { getChild" + "Logger() {} }\n"
            )
        },
    )

    assert _scan(monkeypatch, words) == 0


@pytest.mark.parametrize("kind", ["wheel", "sdist"])
def test_a_built_artifact_is_scanned_inside_as_well_as_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """The checklist names wheels and source archives; a wheel is a zip.

    Pointing the scanner at one used to answer "not a directory", which is the
    worst possible answer for a release gate: it reads as "nothing to see here"
    to anyone who does not read the message. Both findings classes are checked
    here because both live inside the archive.
    """

    marker = "pypi" + ".corp"
    # Assembled from short pieces for the same reason the markers are: a
    # 32-character random-looking literal in this file is exactly what the
    # scanner reports, so the test that proves the scanner works would be the
    # finding that stops the release.
    secret = "Zx7Qm2Lp9Rt4" + "Vw8Bn6Ks3Hd5" + "Yf1Gj0Ac"
    source = _package(
        tmp_path,
        {
            "pkg/registry_url.py": f'INDEX = "https://{marker}/simple"\n',
            "pkg/token.py": f'TOKEN = "{secret}"\n',
        },
    )
    artifact = tmp_path / f"package.{'whl' if kind == 'wheel' else 'tar.gz'}"
    if kind == "wheel":
        with zipfile.ZipFile(artifact, "w") as bundle:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    bundle.write(path, path.relative_to(source))
    else:
        with tarfile.open(artifact, "w:gz") as bundle:
            bundle.add(source, arcname=".")

    assert _scan(monkeypatch, artifact) == 1, "a planted marker and secret were not found"


def test_an_archive_that_hides_nothing_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package(tmp_path, {"pkg/mod.py": "VALUE = 1\n"})
    artifact = tmp_path / "clean.whl"
    with zipfile.ZipFile(artifact, "w") as bundle:
        bundle.write(source / "pkg" / "mod.py", "pkg/mod.py")

    assert _scan(monkeypatch, artifact) == 0


def test_denied_values_are_checked_inside_an_archive_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--deny` is how the credentials being rotated are checked for."""

    # Not a real credential. Assembled from short pieces so this file stays
    # under the scanner's candidate length; the joined value is long and
    # random-looking enough that the entropy check finds it on its own, which
    # is what the second assertion below relies on.
    secret = "deny-" + "eK4mQz8R" + "tW2nXs7Lb3Vp" + "Qd9Hf2"
    source = _package(tmp_path, {"pkg/mod.py": f'TOKEN = "{secret}"\n'})
    artifact = tmp_path / "denied.tar.gz"
    with tarfile.open(artifact, "w:gz") as bundle:
        bundle.add(source / "pkg", arcname="pkg")

    assert _scan(monkeypatch, artifact, "--deny", secret) == 1, (
        "a denied value inside an archive was missed"
    )
    assert _scan(monkeypatch, artifact, "--deny", "not-the-value") == 1, (
        "the planted value is high-entropy on its own, so this asserts the "
        "deny path is an addition rather than a replacement"
    )


def test_a_path_that_is_not_an_archive_is_refused_rather_than_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence would be worse: it would read as a clean result."""

    missing = tmp_path / "nothing-here.whl"

    assert _scan(monkeypatch, missing) == 2


def test_coverage_output_in_a_checkout_is_not_a_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pytest --cov` before the release scan must not produce a scary failure.

    A coverage database records test ids and absolute paths, and the findings it
    produced were high-entropy strings built from this machine's directory
    layout -- never anything a release would contain. The checklist tells a
    releaser to treat a finding as a stop sign, so a false one costs real time.
    """

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    # The shape that tripped it: the base64-ish path runs a SQLite file is full
    # of. Assembled at run time, like the marker strings above, so this file does
    # not itself contain the run the scanner is looking for.
    run = "".join(["Zk9sQmFy", "WlE3dXJ4", "aG1hc3Ru", "b3ppd2Vy", "dGhlcXVp", "Y2tseQ"])
    (checkout / ".coverage").write_bytes(b"\x00sqlite format 3\x00" + run.encode())
    (checkout / ".coverage.host.1234").write_bytes(run.encode())
    (checkout / "coverage.xml").write_text("<coverage><file/></coverage>\n", encoding="utf-8")
    (checkout / "htmlcov").mkdir()
    (checkout / "htmlcov" / "index.html").write_text("VALUE = 1\n", encoding="utf-8")

    assert _scan(monkeypatch, checkout) == 0


def test_a_configuration_file_beside_coverage_output_is_still_scanned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The skip is a name, not a directory: it must not become a hiding place."""

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".coverage").write_bytes(b"\x00binary\n")
    (checkout / "config.yaml").write_text(
        f"endpoint: {_STANDALONE}://collector.internal\n", encoding="utf-8"
    )

    assert _scan(monkeypatch, checkout) == 1
