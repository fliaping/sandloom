# Sandloom release checklist

This is the standalone public repository.
Its fresh history must remain free of private adapters and credentials. Run
release checks against the **exact clean commit** being shipped, not only an
uncommitted working tree. Nothing in this checklist authorizes publishing.

## Before the first public release

- Confirm ownership of the repository and the package-index names
  `sandloom` / `sandloom-runtime`. A local rename does not reserve a PyPI name.
  Until ownership is confirmed, install the locally built wheel pair.
- Set the canonical repository URL in package metadata and OCI labels.
- Enable private vulnerability reporting and identify its response owner in
  `SECURITY.md`. Add real maintainer CODEOWNERS and document security backports;
  do not invent an organization or contact address.
- Review all reachable commits/tags and release artifacts for credentials and
  private material. If a credential was exposed in the original private
  repository, rotate it there; a new public history does not revoke it.
- Keep enterprise adapters in separately versioned private wheels and private
  deployment images. Never add them to the public build context or registry.
- Review README screenshots and generated artwork. They must contain only demo
  data; the architecture illustration must agree with the implemented boundary.

## Source and artifact gates

```bash
uv sync --frozen --all-extras
uv run pytest -q
uv run pytest -q runtime/tests
uv run ruff check src runtime/src tests runtime/tests examples scripts
uv run mypy src runtime/src scripts examples
./scripts/scan_public_tree.py .
./scripts/verify-distributions.sh
./scripts/scan-image.sh
```

The distribution check builds and installs both wheels in a fresh environment,
then imports the installed application and runtime. Publish them together;
the application requires the exact matching runtime version. The source scan
also accepts a wheel or an unpacked source archive. Scan both distributions.

The image scan checks project content, image configuration and layer history.
It is a private-material/secret check, **not** an OS vulnerability scanner.
In-file `scanner:allow-markers` exemptions permit necessary guard-test markers,
not credentials. CI runs these gates; `tests/test_script_contract.py` keeps the
workflow commands and this checklist aligned. A green local run is not proof
that an uncommitted change is present in a release commit.

## Dependency and image supply chain

```bash
./scripts/generate-sbom.sh dist
uv export --frozen --no-dev --all-extras --no-hashes \
  | uvx pip-audit --no-deps --disable-pip --vulnerability-service osv -r /dev/stdin
```

The generated `dist/agent-sandbox.cdx.json` filename remains compatible with
existing tooling. It covers Python dependencies, including adapter extras,
not the OS packages, JDK, Go, Rust or Bubblewrap build. Audit local source
packages separately if the vulnerability service cannot identify them.
Before publishing images, scan those additional components, review findings,
pin image digests and retain build provenance. Sign tags/artifacts according
to the maintainer's release policy. Network scans must be re-run at release
time; an earlier clean result is not a current vulnerability assessment.

## Compatibility without mandatory maximum isolation

- Verify PostgreSQL and MySQL adapters against the versions you advertise.
  Inspect the checked-in SQL schema and controlled upgrade changes.
- Run doctor in each supported worker image/runtime policy and record the
  baseline, additions, network policy and failures.
- A supported basic environment is a valid deployment, not a failed strict
  deployment. Explicit Levels and required additions must fail closed;
  optional additions may be skipped with a reason. Do not remove a baseline
  guarantee to make a test pass.
- Validate advertised combinations where the environment can supply them.
  No requirement says every host must support strict or every middleware.
- Verify the shipped polyglot image, including procfs-free Java/Rust at basic:

  ```bash
  ./scripts/integration-test.sh polyglot
  ```

- Verify a disposable fleet, including stale generations, forwarding,
  worker loss, reclamation and mismatched-profile exclusion:

  ```bash
  export SANDBOX_INTERNAL_TOKEN=$(openssl rand -hex 32)
  SANDBOX_HEARTBEAT_INTERVAL_SECONDS=2 SANDBOX_HEARTBEAT_TTL_SECONDS=10 \
  SANDBOX_MAINTENANCE_INTERVAL_SECONDS=2 SANDBOX_IDLE_TTL_SECONDS=45 \
  SANDBOX_ORPHAN_RELEASE_GRACE_SECONDS=10 \
      docker compose -f compose.fleet.yaml --profile other-profile up -d --build --wait
  uv run python scripts/verify-fleet.py --strict --short-graces
  # Only for this disposable stack; discards its test volumes.
  docker compose -f compose.fleet.yaml --profile other-profile down -v
  ```

  This check stops a worker. Never point it at production. See
  [Integration testing](INTEGRATION_TESTING.md) for requirements and safe usage.

- Load-test the capacity you advertise and document [Sizing](SIZING.md).
  Admission thresholds and per-process RLIMITs are not aggregate sandbox quotas.
- Keep hostile public multi-tenancy claims out of the release unless an
  appropriate independent security assessment supports them. Every Level
  shares the worker kernel; none is a VM boundary.

## Release handoff

Record the commit, application/runtime versions, image digests, tested
platforms, effective profiles, dependency findings and known limitations.
Include upgrade/recovery notes. [Deployment](DEPLOYMENT.md),
[Operations](OPERATIONS.md) and [Environment diagnosis](ENVIRONMENT_DIAGNOSTICS.md)
are the user-facing entry points.
