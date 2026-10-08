#!/usr/bin/env bash
#
# Prove the two published distributions install and run as a pair.
#
# Sandloom's wheels use new distribution names while keeping the existing
# agent_sandbox and agent_sandbox_runtime import packages. Development's local
# source override can hide dependency and package-layout errors, so validate a
# fresh wheel-only install instead:
#
#   ./scripts/verify-distributions.sh
#
# It builds both distributions, installs the runtime wheel first, then the
# application wheel, and asserts that the runtime that ended up installed is the
# one just built. The order matters and is what a user has to do: publishing the
# runtime alongside the application makes the requirement resolvable, and
# installing it first keeps the index out of the picture.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Use the interpreter selected for this project; system python3 may be older
# than our supported range and lack tomllib even when the project tests pass.
VERSION=$(uv run --frozen --no-sync python - <<'PY'
import tomllib
with open("pyproject.toml", "rb") as handle:
    print(tomllib.load(handle)["project"]["version"])
PY
)

WORKDIR=$(mktemp -d)
trap 'rm -rf "${WORKDIR}"' EXIT

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "note: the worktree has uncommitted changes; verifying the tree as it is now." >&2
fi

echo "==> building both distributions (version ${VERSION})"
(cd runtime && uv build --out-dir "${WORKDIR}" >/dev/null)
uv build --out-dir "${WORKDIR}" >/dev/null
ls -1 "${WORKDIR}"

APP_WHEEL="${WORKDIR}/sandloom-${VERSION}-py3-none-any.whl"
RUNTIME_WHEEL="${WORKDIR}/sandloom_runtime-${VERSION}-py3-none-any.whl"
for wheel in "${APP_WHEEL}" "${RUNTIME_WHEEL}"; do
  [[ -f "${wheel}" ]] || { echo "expected ${wheel} to exist" >&2; exit 1; }
done

echo "==> installing into a fresh environment"
uv venv "${WORKDIR}/venv" --python 3.13 >/dev/null
# The runtime first and on its own: it has no dependencies, so nothing is
# fetched for it. Installing the application wheel afterwards finds the pinned
# requirement already satisfied and never asks the index for that name.
uv pip install --python "${WORKDIR}/venv/bin/python" "${RUNTIME_WHEEL}" >/dev/null
uv pip install --python "${WORKDIR}/venv/bin/python" "${APP_WHEEL}" >/dev/null

echo "==> checking what actually got installed"
"${WORKDIR}/venv/bin/python" - "${VERSION}" <<'PY'
import sys
from importlib.metadata import distribution, version

expected = sys.argv[1]

installed = version("sandloom-runtime")
if installed != expected:
    raise SystemExit(
        f"sandloom-runtime {installed} is installed, not {expected}: the "
        "requirement resolved to something other than the distribution built here"
    )

location = distribution("sandloom-runtime").locate_file("agent_sandbox_runtime")
if "site-packages" not in str(location):
    raise SystemExit(f"the runtime did not land in site-packages: {location}")

# The application imports only when the runtime it was built against is the one
# present; a mismatched pair fails here rather than in production.
import agent_sandbox_runtime  # noqa: E402
from agent_sandbox.console import console_html  # noqa: E402

page = console_html()
assert "<!doctype html" in page.lower() and len(page) > 5000, "the console did not ship"

commands = {
    item.name: item
    for item in distribution("sandloom").entry_points
    if item.group == "console_scripts"
}
for name in ("sandloom", "sandloom-doctor", "agent-sandbox"):
    assert name in commands, f"the installed wheel is missing {name}"
    assert callable(commands[name].load()), f"{name} cannot load its entry point"

print(f"    sandloom-runtime {installed} — {agent_sandbox_runtime.__file__}")
PY

"${WORKDIR}/venv/bin/sandloom-doctor" --help >/dev/null
echo "    Sandloom commands and compatibility entry point load"

echo "==> building the application from the installed wheel"
"${WORKDIR}/venv/bin/python" - <<'PY'
import tempfile
from pathlib import Path

from agent_sandbox.app import create_app
from agent_sandbox.config import Settings

root = Path(tempfile.mkdtemp())
app = create_app(
    Settings(
        internal_token="t",
        local_root=root / "sandboxes",
        database_url=f"sqlite+aiosqlite:///{root / 'control.db'}",
        database_auto_ddl=True,
        advertise_host="worker.test",
    )
)
routes = {getattr(route, "path", None) for route in app.routes}
for expected in ("/health", "/healthz", "/admin", "/api/v1/templates"):
    if expected not in routes:
        raise SystemExit(f"the installed application is missing {expected}")
print(f"    {len(routes)} routes, including the console")

# The MCP server is mounted at the root rather than declared as a route, so it
# is checked by importing the module that ships it and confirming it is wired.
from starlette.routing import Mount

from agent_sandbox.mcp_server import MCP_PROXY_PATH

if MCP_PROXY_PATH != "/api/v1/sandboxes/mcp/streamable-http":
    raise SystemExit(f"the MCP path moved: {MCP_PROXY_PATH}")
if not any(isinstance(route, Mount) for route in app.routes):
    raise SystemExit("the installed application does not mount the MCP server")
print("    the MCP server is mounted")
PY

echo ""
echo "OK: both distributions install and run as a pair."
echo "Publish them together, and install the runtime first when installing by hand."
