# Private adapters

This is the recommended way to keep private extensions out of the public tree.
The public repository stays fully usable on its own, while all enterprise-only
SDKs, topology, credentials, and compatibility logic live in a
separate private Python distribution.

## Target layout

```text
public sandloom repository
├── backend protocols and domain models
├── portable SQL, Redis, S3, and Bubblewrap implementations
└── six entry-point loaders

private extension repository
├── private settings and legacy environment translation
├── metadata adapter preserving the existing schema
├── registry and credential adapters
├── runtime mounts, managed environment, and extra probe
├── optional legacy CLI wrapper
└── contract and internal integration tests
```

The public project must not import the private package. The private package
depends on a compatible public-core version and is added only to the internal
image or virtual environment.

## Preserve compatibility without preserving coupling

When adopting the public core alongside an existing deployment, keep behavior stable inside the private
package:

1. Keep the existing table and column names. The private `MetadataStore`
   adapter can query them and return public `agent_sandbox.models.Route`
   objects. Do not rename production tables in the same rollout that changes
   the application architecture.
2. Read the existing environment variables in `PrivateSettings`, then translate
   them into adapter-local configuration. Do not add those fields or aliases to
   public `Settings`.
3. Publish any old console command from the private distribution as a thin
   wrapper around `agent_sandbox.main:main`.
4. Keep enterprise credential retrieval, service discovery, SDK warm-up, and
   topology validation in their respective private factories.
5. Pin the private package to a tested public minor line, for example
   `agent-sandbox>=0.3,<0.4`, and run the public contract suite before upgrading.
   The package may also assert `agent_sandbox.plugins.PLUGIN_API_VERSION == 1`
   during its own startup checks.

This preserves existing data and deployment entry points while making the
dependency direction one-way: private plugin → public core.

## Factory example

```python
from agent_sandbox.config import Settings
from agent_sandbox.runtime import RuntimeExtension, SandboxRuntime
from agent_sandbox_runtime import SandboxMount

from .config import PrivateSettings
from .metadata import LegacyMetadataStore
from .registry import PrivateRegistry


def create_metadata(settings: Settings) -> LegacyMetadataStore:
    return LegacyMetadataStore(settings, PrivateSettings())


def create_registry(settings: Settings) -> PrivateRegistry:
    return PrivateRegistry(settings, PrivateSettings())


def create_runtime(settings: Settings) -> SandboxRuntime:
    private = PrivateSettings()
    return SandboxRuntime(
        settings,
        extension=RuntimeExtension(
            managed_environment=private.managed_environment(),
            mounts=(
                SandboxMount(private.config_source, "/run/company/config", "ro-bind-try"),
            ),
            directories=("/run", "/run/company", "/run/company/config"),
            probe_script=private.runtime_probe_script(),
            capability_labels={"company_runtime": True},
        ),
    )
```

Managed environment names cannot be overwritten by an agent request. Extra
mounts remain subject to the same Bubblewrap command construction, and the
extra probe participates in isolation-level negotiation.

## Internal deployment

Install both wheels, then choose the private implementations explicitly:

```bash
SANDBOX_METADATA_BACKEND=company-metadata
SANDBOX_REGISTRY_BACKEND=company-registry
SANDBOX_OBJECT_STORE_BACKEND=company-objects
SANDBOX_EXECUTION_BACKEND=company-runtime
```

Use a private multi-stage image or an internal dependency lock to add the
plugin. Keep its wheel, lockfile entries, build arguments, package indexes, and
SBOM out of every public build context and CI job.

## Rollout sequence

1. Record the existing deployment as the rollback baseline, including its
   external contract: schema, environment, CLI, health response, and runtime
   behavior.
2. Implement the private adapters in the separate package without changing
   behavior. Run its tests against the existing deployment and the public core.
3. Deploy the public core plus the private plugin with the four selectors above.
   Keep the old schema and deployment variables during this phase.
4. Compare route allocation, generation fencing, command execution, release,
   failover, and audit records in a canary environment.
5. Migrate callers from legacy environment variables and CLI names in later
   releases. Remove compatibility aliases only after usage reaches zero.
6. Treat schema migration as a separate, reversible project if the generic
   public tables are eventually desired.

The rollback is simply the previous image and existing schema; no data rewrite
is required for the architectural cutover.
