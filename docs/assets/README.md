# Documentation visuals

- `admin-fleet.jpg` and `admin-sandboxes.jpg` are unmodified browser captures
  of a disposable local Docker deployment using this repository's source.
  Python, Java and Rust demo workspaces executed real commands. The selected
  policy was `basic` with required `cgroup_namespace` and optional
  `pid_namespace`; PID/proc isolation was unavailable and visibly skipped.
  The demo token is not included in the captures.
- `sandloom-architecture.png` was generated using an image-generation tool
  and checked against the implementation. The exact prompt is recorded in
  [architecture-prompt.txt](architecture-prompt.txt).

These assets accompany the project under its Apache-2.0 license. Screenshots
are examples of one environment's capabilities, not a promise that every
host supports the same profile. Capture new screenshots after material UI or
runtime changes; do not replace unavailable capabilities with invented data.
