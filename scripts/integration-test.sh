#!/usr/bin/env bash
#
# Run the integration suite against real middleware.
#
# Every adapter the project claims to support is verified against an actual
# server rather than a test double, because the behavior under test — row locks,
# dialect JSON handling, TTL expiry, presigned URLs, Linux namespaces — cannot be
# reproduced by a fake.
#
#   ./scripts/integration-test.sh            # everything runnable on this host
#   ./scripts/integration-test.sh middleware # MySQL, PostgreSQL, Redis, S3
#   ./scripts/integration-test.sh sandbox    # Bubblewrap + templates, in a Linux container
#   ./scripts/integration-test.sh polyglot   # the Go/Rust/JDK image, as a deployment
#   ./scripts/integration-test.sh down       # stop and remove the stack
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

COMPOSE_FILE=compose.integration.yaml
BASE_IMAGE=${BASE_IMAGE:-agent-sandbox-base:latest}
TEST_IMAGE=${TEST_IMAGE:-agent-sandbox-test:latest}
# The polyglot variant is a separate artifact with its own tag; the port and
# token are only for the throwaway container this script starts.
POLYGLOT_IMAGE=${POLYGLOT_IMAGE:-agent-sandbox-polyglot:ci}
POLYGLOT_PORT=${POLYGLOT_PORT:-18099}
POLYGLOT_TOKEN=${POLYGLOT_TOKEN:-polyglot-verification-token}

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

start_middleware() {
  echo "==> starting middleware"
  # --wait blocks on the healthchecks, so the suite never races a cold server.
  compose up -d --wait
}

run_middleware_tests() {
  start_middleware
  echo "==> running middleware integration tests"
  uv run pytest tests/integration \
    --ignore=tests/integration/test_bubblewrap_isolation.py \
    "$@"
}

run_sandbox_tests() {
  echo "==> building the Linux test image"
  # The isolation tests need a Linux kernel and root. On macOS or Windows that
  # means a container; on Linux this still isolates the UID range being used.
  if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    echo "--> ${BASE_IMAGE} not found locally, building it"
    docker build -f Dockerfile.base -t "${BASE_IMAGE}" .
  fi
  docker build -f Dockerfile.test --build-arg "BASE_IMAGE=${BASE_IMAGE}" -t "${TEST_IMAGE}" .

  echo "==> running Bubblewrap isolation tests"
  # SYS_ADMIN is what lets bwrap create the namespaces. The suite's whole purpose
  # is to prove the sandbox boundary holds even when the runtime has it.
  #
  # systempaths=unconfined is required because Docker masks paths under /proc by
  # default, which makes `bwrap --proc /proc` fail and forces the probe down to
  # `basic`. A real deployment mounts its own /proc, so without this the runner
  # would silently stop testing the PID and cgroup namespaces.
  #
  # Tests are bind-mounted rather than baked in, so .dockerignore can keep them
  # out of the production image.
  docker run --rm \
    --cap-add SYS_ADMIN \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --security-opt systempaths=unconfined \
    -v "$(pwd)/tests:/app/tests:ro" \
    "${TEST_IMAGE}" \
    uv run --no-sync pytest \
      tests/integration/test_bubblewrap_isolation.py \
      tests/integration/test_template_isolation.py -v "$@"
}

run_polyglot_tests() {
  echo "==> building the polyglot images"
  # The three commands docs/TOOLCHAINS.md gives a reader, in order. Go, Rust and
  # the JDK live in the polyglot base, and nothing else in this repository builds
  # it — so without this run, three of the five documented toolchains are only
  # covered by whoever last built the image by hand.
  #
  # `GO_DIST_BASE` and `GO_SHA256` pass through because dl.google.com, where
  # go.dev redirects, is blocked on some networks. Set them together: a mirror
  # cannot tell you the checksum it should have.
  local build_args=()
  [[ -n "${GO_DIST_BASE:-}" ]] && build_args+=("--build-arg" "GO_DIST_BASE=${GO_DIST_BASE}")
  [[ -n "${GO_SHA256:-}" ]] && build_args+=("--build-arg" "GO_SHA256=${GO_SHA256}")

  if ! docker image inspect "${BASE_IMAGE}" >/dev/null 2>&1; then
    docker build -f Dockerfile.base -t "${BASE_IMAGE}" .
  fi
  # `${build_args[@]+"${build_args[@]}"}` rather than `"${build_args[@]}"`: the
  # array is empty unless those variables are set, and the stock macOS bash
  # (3.2) treats an empty array expansion as an unbound variable under
  # `set -u`. This script runs on macOS more often than anywhere else.
  docker build -f Dockerfile.polyglot ${build_args[@]+"${build_args[@]}"} \
    -t "${BASE_IMAGE%:*}:polyglot" .
  docker build --build-arg "BASE_IMAGE=${BASE_IMAGE%:*}:polyglot" -t "${POLYGLOT_IMAGE}" .

  echo "==> verifying every language at basic without procfs"
  docker run --rm \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    -v "$(pwd)/scripts:/verification:ro" \
    --entrypoint /app/.venv/bin/python \
    "${POLYGLOT_IMAGE}" /verification/verify-polyglot-runtime.py --levels basic

  echo "==> starting it with every toolchain enabled"
  # This second run negotiates higher Levels when possible. The basic run
  # above deliberately leaves Docker's /proc restrictions in place.
  docker rm -f agent-sandbox-polyglot >/dev/null 2>&1 || true
  docker run -d --name agent-sandbox-polyglot \
    --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    --security-opt systempaths=unconfined \
    -e SANDBOX_INTERNAL_TOKEN="${POLYGLOT_TOKEN}" \
    -e SANDBOX_ADVERTISE_HOST=localhost \
    -e SANDBOX_TOOLCHAINS=python,node,go,rust,java \
    -p "${POLYGLOT_PORT}:8080" \
    "${POLYGLOT_IMAGE}" >/dev/null
  trap 'docker rm -f agent-sandbox-polyglot >/dev/null 2>&1 || true' EXIT

  echo "==> waiting for it to serve"
  for _ in $(seq 60); do
    if curl -sf -o /dev/null -H "Authorization: Bearer ${POLYGLOT_TOKEN}" \
      "http://127.0.0.1:${POLYGLOT_PORT}/api/v1/templates"; then break; fi
    sleep 1
  done

  echo "==> verifying it as a client, with the five toolchains"
  SANDBOX_INTERNAL_TOKEN="${POLYGLOT_TOKEN}" \
    uv run python scripts/verify-deployment.py \
      --base-url "http://127.0.0.1:${POLYGLOT_PORT}" --strict
}

case "${1:-all}" in
  middleware)
    shift || true
    run_middleware_tests "$@"
    ;;
  sandbox)
    shift || true
    run_sandbox_tests "$@"
    ;;
  polyglot)
    shift || true
    run_polyglot_tests "$@"
    ;;
  down)
    compose down -v
    ;;
  all)
    shift || true
    run_middleware_tests "$@"
    run_sandbox_tests "$@"
    run_polyglot_tests "$@"
    ;;
  *)
    echo "usage: $0 [middleware|sandbox|polyglot|down|all] [pytest args...]" >&2
    exit 2
    ;;
esac
