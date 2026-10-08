# Security policy

## Supported versions

Security fixes are applied to the latest minor release and the current main
branch during the beta period.

## Reporting a vulnerability

Do not open a public issue containing exploit details, credentials, tenant
data, or a working sandbox escape. Use the repository host's private security
advisory/reporting feature. Include the affected version, isolation level,
outer runtime, minimal reproduction, and impact.

## Threat model

The manager, configuration, database, registry, base image, and host kernel are
trusted. Agent-provided commands, environment values, and workspace files are
untrusted. Authentication and tenant authorization are expected at a trusted
gateway; the built-in bearer token is service-to-service authentication, not
end-user identity.

Bubblewrap namespaces reduce process and filesystem reach but share the host
kernel. Use a VM or microVM backend when the workload is adversarial enough to
require a separate kernel boundary.

## Deployment rules

- Never bake bearer tokens or object-store credentials into images.
- Never expose the Docker socket to sandboxes.
- Never use `--privileged` as a workaround for a failed isolation probe.
- If the outer runtime needs a relaxed seccomp profile for nested namespaces,
  apply it only to the trusted manager and keep agent execution behind a
  successfully negotiated Bubblewrap boundary.
- Keep system mounts read-only and workspace roots tenant-scoped.
- Treat route generation and worker epoch validation as mandatory fencing.
- Put API rate, size, and concurrency limits at the gateway as well as worker.
- Encrypt database, Redis, object-store, and service traffic in untrusted
  networks.
