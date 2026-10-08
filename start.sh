#!/usr/bin/env bash
set -euo pipefail

STARTUP_LOG="${SANDBOX_STARTUP_LOG_PATH:-/tmp/agent-sandbox-startup.log}"
mkdir -p "$(dirname "${STARTUP_LOG}")"
echo "agent-sandbox,status=starting,booting" >> "${STARTUP_LOG}"

on_error() {
    local exit_code=$?
    echo "agent-sandbox,status=failed,bootstrap_exit=${exit_code}" >> "${STARTUP_LOG}"
    exit "${exit_code}"
}
trap on_error ERR

export SANDBOX_PORT="${SANDBOX_PORT:-${AUTO_PORT0:-8080}}"
if ! [[ "${SANDBOX_PORT}" =~ ^[0-9]+$ ]] \
    || [ "${SANDBOX_PORT}" -lt 1 ] \
    || [ "${SANDBOX_PORT}" -gt 65535 ]; then
    echo "invalid HTTP port: ${SANDBOX_PORT}" >&2
    exit 2
fi

if [ -z "${SANDBOX_ADVERTISE_HOST:-}" ]; then
    export SANDBOX_ADVERTISE_HOST="${POD_IP:-${MY_POD_IP:-$(hostname -f)}}"
fi

if [ -z "${SANDBOX_INTERNAL_TOKEN:-}" ]; then
    echo "SANDBOX_INTERNAL_TOKEN is required; generate a random secret and share it with clients" >&2
    exit 2
fi

SANDBOX_ROOT="${SANDBOX_SHARED_ROOT:-${SANDBOX_LOCAL_ROOT:-/var/lib/agent-sandbox/sandboxes}}"
mkdir -p "${SANDBOX_ROOT}"

for executable in /app/.venv/bin/python /usr/bin/bwrap /usr/bin/setpriv /usr/bin/prlimit; do
    if [ ! -x "${executable}" ]; then
        echo "required executable is missing: ${executable}" >&2
        exit 3
    fi
done

echo "agent-sandbox port=${SANDBOX_PORT} advertise_host=${SANDBOX_ADVERTISE_HOST}"
echo "agent-sandbox,status=starting,phase=exec_python,port=${SANDBOX_PORT}" >> "${STARTUP_LOG}"
cd /app
exec /app/.venv/bin/python -m agent_sandbox.main
