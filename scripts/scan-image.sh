#!/usr/bin/env bash
#
# Scan the part of a built image that this project put there.
#
# The release checklist asks whether a published image carries internal markers.
# Pointing `scan_public_tree.py` at an image *root* does not answer that: on an
# image built from this tree alone it reports ~78,000 findings, all of them from
# the base operating system — Debian's keyring armor, the CA bundle, and
# third-party code that happens to contain one of the short markers inside an
# ordinary word ("backlog" carries one, and so does a contributor's name). A gate
# that reports 78,000 findings is a gate nobody runs.
#
# What this project ships inside the image is the build context: the Dockerfile
# copies six paths to /app, and the virtualenv beside them holds third-party
# packages only. So /app is what is scanned — and its presence is verified, since
# the base image has an empty /app of its own and scanning that would report
# "clean" for having looked at nothing. The base operating system is left to the
# image scanner the checklist says is still needed for the OS layer.
#
# `docker export` flattens the filesystem, so it carries neither the layer history
# nor the image configuration — and those are where a build-time internal value
# survives when a build overrides an argument to point at a private mirror.
# BuildKit records the *resolved* build arguments in the history, so
#
#     docker build --build-arg RUSTUP_DIST_SERVER=https://<internal>/rustup ...
#
# leaves that hostname in `docker history` and nowhere in the filesystem at all.
# Both are dumped and scanned for markers: a few kilobytes of JSON and history
# text cost nothing beside the filesystem pass, which over the whole image would
# take minutes to look for values that do not persist there.
#
#   ./scripts/scan-image.sh                       # the image compose would build
#   ./scripts/scan-image.sh agent-sandbox:latest
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

IMAGE=${1:-}
if [[ -z "${IMAGE}" ]]; then
  # The name compose derives from the checkout directory, so a CI workspace with
  # a different path still resolves to the right image.
  IMAGE=$(docker compose config --images 2>/dev/null | head -1 || true)
  if [[ -z "${IMAGE}" ]]; then
    echo "no image given and compose names none; pass one: $0 <image>" >&2
    exit 2
  fi
fi

SCAN_CONTAINER=""
SCAN_WORKDIR=$(mktemp -d)
cleanup() {
  # Only remove the container this invocation created, including its anonymous
  # volumes. Never remove a pre-existing container to claim a fixed scan name.
  if [[ -n "${SCAN_CONTAINER}" ]]; then
    docker rm -f -v "${SCAN_CONTAINER}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${SCAN_WORKDIR}"
}
trap cleanup EXIT

echo "==> exporting ${IMAGE}"
SCAN_CONTAINER=$(docker create "${IMAGE}")
# `export` flattens the layers into the filesystem a process in that container
# would see, which is what a marker in the image would have to survive to matter.
docker export "${SCAN_CONTAINER}" | tar -x -C "${SCAN_WORKDIR}"

# The destination has to be checked, not assumed. An image built before the
# application is copied in still has an empty /app — the base image's WORKDIR —
# and scanning that reports "clean" because it looked at nothing, which is worse
# than no check at all.
if [[ ! -d "${SCAN_WORKDIR}/app/src/agent_sandbox" ]]; then
  echo "${IMAGE} has no application code under /app, so this check would scan" >&2
  echo "nothing and call it clean. If the image is not the service image, or the" >&2
  echo "Dockerfile's layout changed, point this at the new destination." >&2
  exit 2
fi

echo "==> scanning what the build context put in the image"
./scripts/scan_public_tree.py "${SCAN_WORKDIR}/app"

echo "==> scanning the configuration and history the filesystem does not carry"
mkdir -p "${SCAN_WORKDIR}/image-config"
docker inspect "${IMAGE}" >"${SCAN_WORKDIR}/image-config/inspect.json"
# `--format '{{.CreatedBy}}'` is the command line of each layer, with the build
# arguments already substituted, which is the point: on this image these are
# public URLs, but on an image built for an internal mirror the mirror's hostname
# would be here and in no other place.
docker history --no-trunc --format '{{.CreatedBy}}' "${IMAGE}" \
  >"${SCAN_WORKDIR}/image-config/history.txt"
./scripts/scan_public_tree.py --markers-only "${SCAN_WORKDIR}/image-config"
