# Language toolchains

A sandbox runs arbitrary argv, so the runtime is language-neutral. What a
language needs from the platform is small and specific: a package cache, a
registry to fetch from, and a directory on `PATH`. Each language is described
once, in `runtime/src/agent_sandbox_runtime/toolchains.py`, and a deployment
composes the set it wants.

```
SANDBOX_TOOLCHAINS=python,node,go,rust,java
```

The comma form is the one to write; a JSON array still works for deployments
that already use it, and an empty value is refused rather than silently
disabling every language.

## What a toolchain contributes

| | |
|---|---|
| `environment` | Variables applied to every command, such as `GOPROXY` or `CARGO_HOME`. |
| `path_entries` | Prepended to `PATH` in the order the toolchains are listed, so an earlier language shadows a later one. |
| `reserved_names` / `reserved_prefixes` | Variables a caller may **not** override. A caller that can set `GOFLAGS` or `MAVEN_OPTS` can change where a build reads and writes, so those are refused with an error rather than ignored. |

Two conventions hold everywhere:

- **Caches live in `/cache/<tool>`.** They persist with the sandbox and are
  never shared across tenants.
- **User-installed binaries live in `/envs/<tool>`.** A prebuilt environment
  template can supply them read-only — see [TEMPLATES.md](TEMPLATES.md).

## The toolchains

| Name | Ships in | Installs to | Caches in |
|---|---|---|---|
| `python` | base image | `/envs/python-venv`, `/envs/uv-tools` | `/cache/pip`, `/cache/uv` |
| `node` | base image | `/envs/npm-global`, `/envs/pnpm` | `/cache/npm`, `/cache/pnpm`, `/cache/yarn` |
| `go` | polyglot image | `/envs/go` | `/cache/go/mod`, `/cache/go/build` |
| `rust` | polyglot image | `/envs/cargo` | `/cache/cargo-target` |
| `java` | polyglot image | `/envs/java` | `/cache/maven`, `/cache/gradle` |

TypeScript needs no toolchain of its own: `tsc` and the runners that execute
TypeScript directly are npm packages, so they install into the `node`
directories.

Only `python` and `node` are enabled by default, and only they are in the base
image. A deployment whose agents write Go, Rust, or JVM code builds the
polyglot variant instead:

```sh
docker build -f Dockerfile.base -t agent-sandbox-base:latest .
docker build -f Dockerfile.polyglot -t agent-sandbox-base:polyglot .
docker build --build-arg BASE_IMAGE=agent-sandbox-base:polyglot -t agent-sandbox .
```

`./scripts/integration-test.sh polyglot` runs those three builds, starts the
result with all five toolchains enabled, and verifies it as a client — which
means compiling and running a program in each language, from a sandbox. It is
also what CI's `Polyglot image` workflow runs, so the three toolchains that exist only
in this variant are covered rather than trusted.

On a network that blocks `dl.google.com`, where go.dev sends its archives, pass
`GO_DIST_BASE=https://mirrors.aliyun.com/golang` and the script forwards it to
the build. The Dockerfile pins the official digest of the version it installs,
so the mirror has to serve the official bytes — a mirror does not weaken the
check, and no second argument is needed. A version this file does not pin takes
its digest from go.dev's release index instead, and a build that cannot reach
that either passes the `GO_SHA256` it looked up.

Enabling a toolchain whose language is not in the image gives a sandbox a
`PATH` entry and a cache for a compiler that is not there.

## All shipped languages work at basic

The current polyglot image supports Java and Rust at `basic` as well as
Python, JavaScript and Go. TypeScript uses Node and an installed compiler or
runner; it is not installed by default. Select the isolation Level for the
workload's process boundaries, independently of the language.

`basic` still mounts no procfs and adds no PID namespace. The image installs
adapters using `scripts/install-polyglot-launchers.py`:

- **Java:** a JDK-shaped facade at `/usr/local/lib/agent-sandbox/java` supplies
  wrappers for `java`, `javac`, `jar` and the other installed JDK tools. Each
  wrapper supplies the real JDK library path only to its child process.
  `SANDBOX_JAVA_HOME` points both `PATH` and `JAVA_HOME` at this facade, so
  Maven and project wrappers also use the adapted `java` executable.
- **Rust:** `/usr/local/lib/agent-sandbox/rust/bin` calls the image-pinned
  `rustc`, `cargo` and `rustdoc` directly, bypassing rustup's self-locating
  proxies. Compiler/doc launchers supply an explicit sysroot and library
  path; Cargo selects those adapted compiler/doc executables.
  `SANDBOX_RUST_LAUNCHER_DIR` inserts this directory before rustup's proxies.

These paths are inside the existing read-only `/usr` mount. Libraries are not
added to the global sandbox environment, and no host `/proc` is exposed.
Both image settings are optional: leaving them unset preserves custom/legacy
images' original launch paths and their measured availability.

The procfs-free Rust adapters use the toolchain selected at image build time.
They do not interpret `rust-toolchain.toml` or directory rustup overrides;
`cargo +nightly` and similar selectors return a clear error. Build an image
with the required `RUST_VERSION` for a different compiler. Rustup remains
installed for image administration, but toolchain downloads and switching
are not provided by these sandbox launchers.

Older polyglot images without the adapters may still fail at `basic` with
`libjli.so` or `/proc/self/exe` errors. Rebuild the polyglot and service images
to pick up the adapters.

Libraries, native extensions, or JVM diagnostic/attach tools that explicitly
use `/proc` can still require `standard`. The checks cover source compilation,
execution and the documented build paths, not every possible third-party
tool. Already-compiled Rust programs depend on their own runtime needs.

## Measured availability

After negotiating isolation, startup launches each enabled toolchain in a
temporary sandbox with the selected Level, UID, mounts, and environment.
This does not download packages or compile projects. Each check has a
10-second deadline, and the temporary sandbox and caches are removed afterwards.

`/healthz` publishes these results at `worker.capabilities.toolchains.checks`.
Each entry has `check: "launch"` and one of `available`, `missing`, `failed`,
or `timed_out`; `detail` contains bounded version output or the failure reason.
When Node is configured, TypeScript is checked as an optional installed
`tsc` compiler. A missing optional compiler does not produce a startup warning.

The `available` list is measured. `isolation_hints` retains the static procfs
hints, while `unavailable_at_this_level` contains only hinted languages whose
launch actually failed. A working custom Java/Rust launcher at `basic` is
therefore reported as available. None of these checks upgrades or weakens the
requested isolation Level, and language failures are diagnostic rather than
a service-wide startup gate.

`available` means the checked tools start, not that every dependency or project
build works. The deployment verifier compiles and runs source code in both
plain and login shells; it additionally checks TypeScript when installed.

A failed tool launch is reported after isolation negotiation, for example:

```
preflight warning: toolchain java: sandbox launch probe failed at isolation
level 'basic'; ... libjli.so ... See docs/TOOLCHAINS.md.
```

To reach `standard`, add `systempaths=unconfined` alongside the
`seccomp=unconfined` the Compose file already sets:

```yaml
# compose.override.yaml
services:
  agent-sandbox:
    security_opt: !override
      - seccomp=unconfined
      - systempaths=unconfined
```

`!override` is required because Compose rejects a merged list that repeats
`seccomp=unconfined`. This unmasks `/proc` paths for the trusted manager
container, which is a wider surface than Docker's default profile, so it is
deliberately left out of the default Compose file — see
[ISOLATION.md](ISOLATION.md#docker-and-compatible-runtimes) for what it trades.

`auto` picks the highest Level the container permits. The current adapters
keep Java and Rust working even when this resolves to `basic`.

## Verifying a deployment

`./scripts/integration-test.sh polyglot` first runs
`scripts/verify-polyglot-runtime.py --levels basic` in the built worker image.
It verifies `/proc/self/exe` is absent, then compiles/runs each installed
language in plain and login shells. Java additionally creates and executes a
JAR and validates a Maven project offline. Rust additionally tests a local
dependency, build script, unit tests, doctests and documentation generation
offline. The second run verifies the HTTP deployment with higher Levels
available.

```sh
export SANDBOX_INTERNAL_TOKEN=...      # the value the service was started with
./scripts/verify-deployment.py                     # or --base-url http://host:8080
```

It drives a real service over HTTP — the same calls a client makes — covering
the lifecycle, the file API, templates, and the MCP toolset, and it compiles and
runs a program in each enabled language inside a sandbox, through a login shell
and a plain one. That last part is not decoration: a compiler can be
installed, on the image's `ENV PATH`, and still be missing from the sandbox,
because the runtime composes `PATH` itself. Nothing that only reads files will
notice.

Every enabled language it cannot check is reported as SKIP with the reason, and
`--strict` turns a SKIP into a failure so a partial run cannot pass quietly.
