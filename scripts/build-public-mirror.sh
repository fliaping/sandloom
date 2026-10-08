#!/usr/bin/env bash
#
# Build a clean single-commit mirror that is safe to publish.
#
# The repository history contains credentials that were removed from the tree
# but are still reachable in commits. Deleting a file does not revoke a secret,
# and publishing the current history would republish it — see
# docs/OPEN_SOURCE_RELEASE.md, "Hard blockers". This exports the tracked tree
# from HEAD into a fresh repository with one commit and no history at all,
# then scans the result and refuses to leave it in place if anything is found.
#
#   ./scripts/build-public-mirror.sh ../agent-sandbox-public
#   ./scripts/build-public-mirror.sh ../agent-sandbox-public --deny-file ~/secrets-to-check.txt
#   ./scripts/build-public-mirror.sh ../agent-sandbox-public --verify
#   ./scripts/build-public-mirror.sh ../agent-sandbox-public --verify --packages
#
# --verify then installs the mirror from its own lockfile and runs both test
# suites inside it, so what is about to be published is exercised the way a
# user receives it instead of the way the working tree happens to be. The
# difference is not academic: a fix left uncommitted passes in the worktree and
# fails in the export, and this branch reached the point of being publishable
# with exactly such a change sitting in it.
#
# --packages additionally builds the wheels in the mirror and installs them into
# a fresh virtualenv, which is the slower half and the only part that needs the
# network.
#
# The mirror is built from HEAD, so commit or stash first; the script refuses
# to run against a dirty worktree rather than silently building a mirror that
# does not match what was reviewed.
#
# --deny-file names a local, untracked file of newline-separated values that
# must not appear in the result — the credentials being rotated, for example.
# It is read at run time and never written into any repository, because a
# scanner that hard-codes a leaked token has leaked it again.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

TARGET=${1:-}
if [[ -z "${TARGET}" ]]; then
  echo "usage: $0 <target-directory> [--deny-file <path>]" >&2
  exit 2
fi
shift

VERIFY=0
PACKAGES=0
DENY_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --deny-file)
      DENY_FILE=${2:-}
      [[ -f "${DENY_FILE}" ]] || { echo "no such file: ${DENY_FILE}" >&2; exit 2; }
      # Read line by line so a value containing spaces survives intact.
      while IFS= read -r value; do
        [[ -n "${value}" ]] && DENY_ARGS+=(--deny "${value}")
      done < "${DENY_FILE}"
      shift 2
      ;;
    --verify)
      VERIFY=1
      shift
      ;;
    --packages)
      # The wheels are the slower half, and there is no point building them for a
      # tree that does not pass its suites.
      VERIFY=1
      PACKAGES=1
      shift
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "the worktree has uncommitted changes; commit or stash them first." >&2
  echo "a mirror built now would not match what you reviewed." >&2
  exit 1
fi

if [[ -e "${TARGET}" ]] && [[ -n "$(ls -A "${TARGET}" 2>/dev/null)" ]]; then
  echo "${TARGET} already exists and is not empty; refusing to overwrite." >&2
  exit 1
fi

echo "==> exporting the tracked tree from HEAD (no history)"
mkdir -p "${TARGET}"
# git archive writes exactly the tracked files at HEAD: no .git, no ignored
# files, no untracked scratch. That is what makes the mirror history-free.
git archive HEAD | tar -x -C "${TARGET}"

echo "==> scanning the exported tree"
# `${DENY_ARGS[@]+"${DENY_ARGS[@]}"}` rather than `"${DENY_ARGS[@]}"`: the array
# is empty unless a --deny-file was given, and the stock macOS bash (3.2)
# treats an empty array expansion as an unbound variable under `set -u`. The
# documented invocation passes no --deny-file, so the failure would be the
# common case, and it lands after the tree has been extracted.
if ! ./scripts/scan_public_tree.py "${TARGET}" ${DENY_ARGS[@]+"${DENY_ARGS[@]}"}; then
  echo "" >&2
  echo "Leaving ${TARGET} in place for inspection, but do not publish it." >&2
  exit 1
fi

echo "==> creating a single commit"
git -C "${TARGET}" init -q -b main
git -C "${TARGET}" add -A
git -C "${TARGET}" -c user.name="release" -c user.email="release@localhost" \
  commit -q -m "Import from the reviewed internal tree

Squashed single commit. The upstream history contains credentials that were
removed from the tree but remain reachable in commits, so it is deliberately
not carried over."

echo ""
echo "==> done: ${TARGET}"
git -C "${TARGET}" log --oneline

if [[ "${VERIFY}" -eq 1 ]]; then
  echo ""
  echo "==> verifying the mirror as a user receives it"
  (cd "${TARGET}" && uv sync --frozen >/dev/null)
  failed=0
  (cd "${TARGET}" && uv run pytest -q) || failed=1
  (cd "${TARGET}" && uv run pytest -q runtime/tests) || failed=1
  if [[ "${PACKAGES}" -eq 1 ]]; then
    (cd "${TARGET}" && ./scripts/verify-distributions.sh) || failed=1
  fi
  if [[ "${failed}" -ne 0 ]]; then
    echo "" >&2
    echo "The mirror was built and scanned clean, but it does not pass its own" >&2
    echo "checks, so publishing it would publish a red tree. Fix that on the" >&2
    echo "reviewed branch, commit it, and build the mirror again." >&2
    exit 1
  fi
  echo "OK: the mirror installs from its lockfile and passes both suites."
fi

echo ""
echo "Before publishing, still confirm the items in docs/OPEN_SOURCE_RELEASE.md"
echo "that a scan cannot check — rotated credentials, signing, and the"
echo "third-party security review. Then:"
echo ""
echo "  git -C ${TARGET} remote add origin <url>"
echo "  git -C ${TARGET} push -u origin main"
