# Contributing

Contributions are welcome. Keep the generic core free of organization-specific
SDKs, endpoints, credentials, and deployment assumptions. Add private-platform
behavior behind an explicit adapter.

Before submitting a change:

1. Add tests for lifecycle races, fencing, path validation, or capability
   downgrade behavior affected by the change.
2. Run what CI runs, with the arguments it uses:

   ```bash
   uv run pytest -q                       # the control plane
   uv run pytest -q runtime/tests         # the runtime distribution
   uv run ruff check src runtime/src tests runtime/tests examples scripts
   uv run mypy src runtime/src scripts examples
   ```

   Both test commands are needed: the repository holds two distributions, and
   `testpaths` in the root `pyproject.toml` collects `tests/` only, so a plain
   `pytest` says nothing about the runtime package.
3. Document new configuration and its safe default in
   [docs/CONFIGURATION.md](docs/CONFIGURATION.md). A test fails if a setting is
   missing from it, or if a variable the documentation names is not a setting.
4. For a new backend, describe its isolation boundary and outer-runtime
   prerequisites without recommending broad privileges.

Anything that needs Docker is not part of every pull request and has to be run
deliberately: `./scripts/integration-test.sh middleware` against real
MySQL/PostgreSQL/Redis/S3, `sandbox` for Bubblewrap and templates, and
`polyglot` for the Go/Rust/JDK image — see
[docs/INTEGRATION_TESTING.md](docs/INTEGRATION_TESTING.md). A change to how the
service starts or serves should also be checked the way CI's deployment job
checks it: the README quick start, then
`./scripts/verify-deployment.py --strict` against it.

Schema changes must work on MySQL and PostgreSQL or include clearly separated
dialect migrations. Security-sensitive changes should include a short threat-
model note in the pull request.
