# Environment templates

## The problem

A sandbox starts with an empty `/envs`. Every application that needs NumPy, or a
`node_modules` tree, or a compiler toolchain pays the install cost again — and
the same dependency set gets rebuilt thousands of times across a fleet.

Baking environments into the container image is not a real answer either. Images
are the wrong granularity: one team's CUDA stack, another's pinned `pandas`, a
third's Rust toolchain. Rebuilding and redeploying an image per combination does
not scale, and it makes the environment the slowest-moving part of the system.

A template is an environment tree built once and mounted read-only into every
sandbox that asks for it.

## Quickstart

`examples/templates.py` is this quickstart as a script that runs: it builds a
virtualenv in one sandbox, publishes it, mounts it into a second sandbox, and
checks that the mount is read-only. Start there if you would rather run
something than read something.

All calls take `Authorization: Bearer $SANDBOX_INTERNAL_TOKEN`; it is elided
below. Build an environment once and promote it:

```bash
# 1. Resolve and create a sandbox to build in. `resolve` returns the generation
#    that every later call has to echo back.
POST /api/v1/sandboxes/resolve
{"sandbox_id": "env-builder", "workspace_scope_id": "team/data"}
# -> {"generation": 1, ...}

POST /api/v1/sandboxes/env-builder
{"generation": 1}

# 2. Install the environment. Anything that lands under /envs works.
POST /api/v1/sandboxes/env-builder/exec
{"exec_id": "mkenv", "generation": 1,
 "argv": ["sh", "-lc", "python3 -m venv /envs/python-ml && /envs/python-ml/bin/pip install numpy pandas"],
 "timeout_seconds": 600}

# 3. Promote it to a named template. The response carries the digest.
POST /api/v1/sandboxes/env-builder/templates
{"generation": 1, "name": "python-ml", "source_path": "/envs/python-ml",
 "description": "Python 3.13 + numpy + pandas"}
# -> {"name": "python-ml", "digest": "sha256:...", "mount_target": "/envs/python-ml", ...}
```

Any sandbox, on any worker, can then mount it:

```bash
PUT /api/v1/sandboxes/my-agent/templates
{"generation": 4, "templates": ["python-ml"]}

POST /api/v1/sandboxes/my-agent/exec
{"exec_id": "check", "generation": 4,
 "argv": ["/envs/python-ml/bin/python", "-c", "import numpy; print(numpy.__version__)"]}
```

List and unpublish:

```bash
GET    /api/v1/templates
DELETE /api/v1/templates/python-ml
```

`DELETE` removes the name from the catalog, and nothing else: the archive stays
in the object store, and a sandbox that already has the revision attached keeps
running. Each of its commands rebuilds the mount list from the revision recorded
on the sandbox rather than from the name, so it still mounts the tree it asked
for. What the delete does stop is anyone *starting* to use the name: resolution
begins at the catalog, so a new attach fails, and so does one that pins a digest
— a pin names a revision, but the name still has to exist for it to be found.

That makes retirement one-way, which is the intent, and recoverable, which is
why it is not destructive: the environment is still a tree in the builder sandbox
or in your build pipeline, and publishing identical content yields the same
digest, so restoring the name restores the exact revision that was retired.

## Another interpreter, or another language

The environment a team needs is usually not the one on `PATH`. The image
carries two Pythons — the platform interpreter this project provides (3.13,
first on `PATH`) and Debian's own (3.11, at `/usr/bin`) — and the same recipe
builds a template out of either, or out of anything you install under `/envs`:

```bash
# In the builder sandbox: a virtualenv from the distribution's interpreter.
uv venv --python /usr/bin/python3.11 /envs/py311
uv pip install --python /envs/py311/bin/python -r /workspace/requirements.txt

# Then publish it, as above, and every sandbox on the fleet mounts it:
#   POST /api/v1/sandboxes/env-builder/templates
#   {"generation": 1, "name": "py311", "source_path": "/envs/py311"}
```

Two things about this are not obvious, and both are why `uv` is the tool rather
than `python -m venv`:

- **`python3.11 -m venv` produces a virtualenv without `pip`.** Debian splits
  `ensurepip` into a separate package that is not installed in this image, so
  `venv` succeeds and then `bin/pip` does not exist. `uv venv` does not need
  `ensurepip`.
- **The distribution interpreter is `EXTERNALLY-MANAGED`.** `pip install` into
  it is refused by design; install into the virtualenv instead, which is what
  `uv pip install --python /envs/<name>/bin/python` does.

Anything else follows the same shape: an interpreter, a `node_modules` tree, a
Rust target directory, a JDK — whatever lands in a directory under `/envs` can
be published and mounted. The polyglot image already ships Go, Rust and the JDK
inside every sandbox, so those need no template at all; a CPython version this
project does not provide, or a dependency set your application pins, does.

The deployment verifier runs this recipe against every deployment it checks — it
builds a virtualenv from `/usr/bin/python3.11` and runs the mounted interpreter
in a second sandbox — so the commands above cannot drift from what the service
actually does.

## How it works

```
build                     publish                    use
─────                     ───────                    ───
sandbox /envs/x    →   tar.gz + sha256   →   object store   →   worker cache
                          ↓                                          ↓
                    catalog: x → digest                    ro-bind → /envs/x
```

Four properties make this work, and each is deliberate:

**Content-addressed.** A template's identity is the SHA-256 of its archive, so a
cached copy is provably the right one, two workers can never disagree about what
a name means, and re-materializing is idempotent. Archives are reproducible —
mtimes, ownership, and the gzip header are all zeroed — so rebuilding an
identical tree yields an identical digest and hits the cache instead of
invalidating it.

**Name the template after the directory you built it from.** A template always
mounts at `/envs/<name>`, so a tree built at `/envs/python-ml` has to be
published as `python-ml` for its path to mean the same thing at build time and
at use time. That equivalence is the whole reason templates live under `/envs`
rather than being unpacked somewhere convenient.

Getting this wrong fails in a specific and quiet way. A virtualenv still runs
when mounted at the wrong path, because `pyvenv.cfg` is resolved relative to
the interpreter binary, so `bin/python` keeps working and an experiment looks
successful — but every entry point records an absolute shebang
(`#!/envs/python-ml/bin/python3`), and those break. `bin/pip` and any console
script stop being executable. Verified: a venv built at `/envs/myenv` and
published as `other` runs `bin/python` fine from `/envs/other`, and fails
`/envs/other/bin/pip` with `not found`.

**Read-only, and shared.** Every sandbox on a worker bind-mounts the same tree,
so the pages are shared in the page cache and the marginal cost of the tenth
sandbox using a template is approximately zero. Read-only also means one tenant
cannot poison another's environment. A sandbox that needs to modify a template
builds a new one — that is the intended workflow, not a limitation.

The store is also what carries a template past one worker. Every deployment
pointed at the same endpoint, bucket and `BLOBSTORE_BASE_PREFIX` reads the same
catalog, so an environment built once in one cluster is resolvable in another,
and the prefix is what keeps two teams sharing a bucket apart. Verified end to
end: a template published in one cluster, attached by name in a second
deployment that shared nothing else with it, and the file it shipped read inside
that sandbox.

**Resolved before the sandbox starts using it.** Attaching materializes the
template immediately, so a missing or corrupt template fails the call that asked
for it rather than an unrelated command later.

## Pinning

`templates: ["python-ml"]` follows the catalog, so it silently picks up new
revisions. `templates: ["python-ml@sha256:<hex>"]` pins an exact revision and is
what you want for a reproducible run; the digest comes back from the build call.
A pin may reference a revision older than the one the catalog points at, which is
the point — but the name still has to exist in the catalog for it to resolve.

Because the target is the name, two different names never collide — but two
*revisions* of one name do, and pinning both into one sandbox
(`["demo@sha256:...", "demo@sha256:..."]`) is rejected rather than resolved by
ordering. Listing the same revision twice is not: the list is a set, and a caller
that unions several capability sets repeats itself.

## Failure modes

| Symptom | Cause |
| --- | --- |
| `template 'x' is not in the catalog` | Never published, unpublished, or published on a worker without a shared object store |
| `digest mismatch` | Object store holds different bytes than the catalog claims; a corrupted or overwritten object |
| `no object store is configured to fetch it from` | The template was built on another worker and there is nothing to fetch it from |
| `/envs/x/some/file: Permission denied` | The tree kept the modes the building sandbox recorded, and the sandbox that mounted it runs as a different UID. Read [Permissions](#permissions) and make the tree readable before publishing |
| `SANDBOX_TEMPLATES_UNSUPPORTED` | The configured execution backend does not implement templates |
| `OBJECT_STORE_UNAVAILABLE` | The bucket does not exist and could not be created, or the store is unreachable. The log line names the bucket, the endpoint and the S3 error |

Templates need an object store to cross workers. Without one they still work, but
only on the worker that built them — useful for a single-node install, which is
why templates do not require S3.

Naming an endpoint and a bucket is enough to start: every worker creates the
bucket at startup when the store does not have it, which is the state a new S3
account, a MinIO install and a LocalStack container all begin in. A store that
cannot be reached, or credentials that may read but not create, are logged at
startup and left for the operation that needs them — the first publish then
answers `503 OBJECT_STORE_UNAVAILABLE`, and the same line is in the log. A bucket
that is missing is never reported as a template that is missing: "nothing
published yet" and "no worker can read the store" are different answers, and a
catalog that confuses them tells every worker a team's environments do not exist.

## Permissions

A revision is unpacked as data and mounted read-only, and it keeps the modes the
building sandbox recorded for it. What materialization adds is traversal: every
directory in the tree gets `x` for everyone, because the sandboxes that mount it
run as unprivileged UIDs that do not own the cache and one non-enterable
directory is enough to make the tree unreachable. Nothing else is touched, read
bits included — an environment that ships a key still ships it with the mode its
author gave it, and a template is shared with every tenant that mounts it.

The case to know about is a tree prepared through the File API.
`PUT /api/v1/sandboxes/{id}/files` writes the file `0600` and owned by the
sandbox, deliberately: a worker's disk is not world-readable. A template
published from such a tree mounts, attaches, and then answers `Permission
denied` to every other sandbox, because those run as different UIDs and the read
bit is not there. Make the tree readable before publishing it:

```bash
# In the builder sandbox, before the POST that publishes it.
chmod -R a+rX /envs/python-ml
```

`a+rX` adds read to files and directories and adds `x` only where it already
existed or where the entry is a directory, so the environment keeps its shape
while becoming usable. An environment installed by a package manager, a
compiler, or a virtualenv needs none of this: those write world-readable files,
which is why the quick start does not mention permissions.

## Operational notes

- **Where the cache lives.** `SANDBOX_TEMPLATE_ROOT`, defaulting to a `templates`
  sibling of the workspace root. A sibling rather than a child, so the workspace
  validator never sees it and no sandbox can reach it through its own tree. Put it
  on a filesystem with room for every environment your tenants use at once.
- **Disk.** The cache is digest-addressed and keeps every revision of a name, so
  a rollback is a remount rather than a rebuild. The copy is per *name*: the
  object store holds one archive for identical content, but a worker that has
  the same tree mounted under two names holds two directories, because the
  revision a sandbox keeps alive is identified by both. Set `SANDBOX_TEMPLATE_CACHE_MAX_BYTES`
  to enable LRU eviction; revisions bound to a live sandbox are never evicted, and
  a revision stops being pinned when the sandbox it was mounted into is destroyed.
  The worker sweeps the cache on its maintenance cycle, which is the same cycle
  that reclaims workspaces, so eviction lags a burst of pulls by at most
  `SANDBOX_MAINTENANCE_INTERVAL_SECONDS`.
- **Size limits.** `SANDBOX_TEMPLATE_MAX_EXTRACT_BYTES` (8 GiB) and
  `SANDBOX_TEMPLATE_MAX_ARCHIVE_BYTES` (4 GiB) bound a single template. The
  extract limit matters more than the archive limit: a small archive can expand
  arbitrarily, and the cache disk is shared.
- **What may be a source.** Only `/envs` and `/workspace`. `/home` and `/cache`
  are excluded because they hold credentials and throwaway state, and a template
  is shared with every tenant that mounts it.
- **Secrets.** Nothing scans a template for credentials, and file read bits are
  preserved exactly as the archive recorded them. Do not promote an environment
  with a token baked into it.

## API summary

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/sandboxes/{id}/templates` | Build a template from a sandbox path |
| `PUT` | `/api/v1/sandboxes/{id}/templates` | Attach templates to a sandbox |
| `GET` | `/api/v1/templates` | List published templates |
| `DELETE` | `/api/v1/templates/{name}` | Unpublish a name |
