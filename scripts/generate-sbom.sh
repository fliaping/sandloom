#!/usr/bin/env bash
#
# Generate the CycloneDX SBOM for the Python content of a release.
#
#   ./scripts/generate-sbom.sh [outdir]      # default: dist
#
# `uv export --format cyclonedx1.5` does the work. This adds the four things a
# *published* SBOM needs and the raw export cannot know:
#
#   * `--all-extras --no-dev` is the set the image installs — the Dockerfile runs
#     `uv sync --frozen --no-dev --all-extras`. Without `--all-extras` the file
#     would omit aiomysql, asyncpg, pymysql and redis: the database and registry
#     drivers, which are the components a scanner most needs to see. Without
#     `--no-dev` it would list pytest, mypy and ruff, which are not in the image.
#   * `--frozen`, so the file describes the lock that ships rather than one
#     re-resolved at export time.
#   * The runtime is a path dependency inside this repository, so the export
#     emits it with no purl — as though it were not a distribution anyone could
#     look up. In a release it is one, and this project has already had to be
#     careful about that exact name (docs/OPEN_SOURCE_RELEASE.md). The purl is
#     added only when the locked version matches the project version, so the
#     file cannot describe a pair that was never built.
#   * The root component is `uv`'s synthetic project node. It is replaced with
#     the distribution the SBOM is actually about.
#
# Scope: the Python packages the image runs. The OS layer — the base image, the
# Debian packages, the Bubblewrap build — needs an image scanner as well, and the
# wheels declare fewer dependencies than the image (extras are opt-in at install
# time), so a wheel-scoped SBOM is a different document with different flags.
#
# `uv export --format cyclonedx1.5` is experimental in uv, which is why there is
# a test asserting this file's shape: if the export changes, that fails loudly
# instead of publishing a document that silently describes the wrong thing.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

OUTDIR=${1:-dist}
mkdir -p "${OUTDIR}"
OUT="${OUTDIR}/agent-sandbox.cdx.json"

uv export --format cyclonedx1.5 --frozen --all-extras --no-dev \
  --output-file "${OUT}.raw" >/dev/null

# Use the project's supported interpreter, not an arbitrary system python3
# (macOS may still provide Python 3.9, which has no tomllib). No sync is needed
# to parse stdlib-only metadata from the frozen lock.
uv run --frozen --no-sync python - "${OUT}.raw" "${OUT}" <<'PY'
import json
import sys
import tomllib
from pathlib import Path

raw, target = Path(sys.argv[1]), Path(sys.argv[2])
with open("pyproject.toml", "rb") as handle:
    project = tomllib.load(handle)["project"]
with open("runtime/pyproject.toml", "rb") as handle:
    runtime_project = tomllib.load(handle)["project"]

name, version = project["name"], project["version"]
runtime_name = runtime_project["name"]

document = json.loads(raw.read_text())
document["metadata"]["component"] = {
    "type": "application",
    "bom-ref": f"pkg:pypi/{name}@{version}",
    "name": name,
    "version": version,
    "purl": f"pkg:pypi/{name}@{version}",
}

for component in document["components"]:
    if component["name"] != runtime_name:
        continue
    if component["version"] != runtime_project["version"] or version != runtime_project["version"]:
        raise SystemExit(
            f"{runtime_name} is {component['version']} in the lock and "
            f"{runtime_project['version']} in its own project file while {name} is "
            f"{version}: the pair has to be published together, so the SBOM would "
            "name a combination that was never built"
        )
    component["bom-ref"] = f"pkg:pypi/{runtime_name}@{version}"
    component["purl"] = component["bom-ref"]

target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
print(f"{len(document['components'])} components, {name} {version}")
PY

rm -f "${OUT}.raw"
echo "wrote ${OUT}"
