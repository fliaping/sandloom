"""The admin console, served as one self-contained page.

Deliberately dependency-free: no build step, no npm, no CDN. An operator
console is exactly the thing you need when a cluster is misbehaving, which is
also when a locked-down or air-gapped network is least willing to fetch a
font or a framework from the internet. That rules out webfonts and bundlers,
so the page relies on a system font stack and vanilla JavaScript.

It also means the console cannot drift out of sync with the API version that
ships it, and that `pip install agent-sandbox` is the whole installation.
"""

from __future__ import annotations

from functools import lru_cache

_CONSOLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sandloom · fleet console</title>
<style>
  /* Instrument-panel aesthetic: warm paper ground, one rust accent, dense
     tabular data. Neutrals are tinted toward the accent hue rather than
     pure gray, so the surface reads as a deliberate material. */
  :root {
    --ground: oklch(97.2% 0.008 70);
    --surface: oklch(99.4% 0.005 70);
    --sunk: oklch(94.6% 0.011 70);
    --line: oklch(88% 0.014 70);
    --line-firm: oklch(78% 0.02 70);
    --ink: oklch(24% 0.02 60);
    --ink-soft: oklch(48% 0.018 65);
    --ink-faint: oklch(51% 0.015 68);
    --accent: oklch(52% 0.152 42);
    --accent-soft: oklch(94% 0.036 52);
    --ok: oklch(52% 0.11 152);
    --warn: oklch(64% 0.13 78);
    --bad: oklch(52% 0.17 25);
    --idle: oklch(64% 0.014 70);
    --shell: oklch(26% 0.022 58);
    --step: 4px;
    --sans: "Avenir Next", "Segoe UI Variable Display", "Optima", "Gill Sans",
            "Lucida Grande", sans-serif;
    --data: "SF Mono", "IBM Plex Mono", "Cascadia Mono", "Consolas", monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ground: oklch(19% 0.014 60);
      --surface: oklch(23% 0.016 60);
      --sunk: oklch(16.5% 0.012 60);
      --line: oklch(31% 0.018 60);
      --line-firm: oklch(42% 0.022 60);
      --ink: oklch(93% 0.012 70);
      --ink-soft: oklch(74% 0.015 70);
      --ink-faint: oklch(72% 0.015 70);
      --accent: oklch(70% 0.14 48);
      --accent-soft: oklch(30% 0.05 45);
      --ok: oklch(72% 0.14 152);
      --warn: oklch(78% 0.14 80);
      --bad: oklch(66% 0.17 25);
      --idle: oklch(50% 0.014 70);
      --shell: oklch(14% 0.01 60);
    }
  }
  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--ground); color: var(--ink);
    font-family: var(--sans);
    font-size: 15px; line-height: 1.5;
    font-variant-numeric: tabular-nums;
  }
  /* ── shell ── */
  header.bar {
    background: var(--shell); color: oklch(96% 0.01 70);
    display: flex; align-items: baseline; gap: calc(var(--step) * 6);
    padding: calc(var(--step) * 3) clamp(16px, 3vw, 40px);
    border-bottom: 3px solid var(--accent);
    flex-wrap: wrap;
  }
  .mark { font-size: 15px; font-weight: 600; letter-spacing: 0.14em; text-transform: uppercase; }
  .mark b { color: var(--accent); font-weight: 600; }
  nav { display: flex; gap: calc(var(--step) * 5); margin-left: auto; flex-wrap: wrap; }
  nav button {
    background: none; border: 0; padding: 4px 0; min-height: 28px;
    color: oklch(72% 0.012 70); font: inherit; font-size: 13px;
    letter-spacing: 0.08em; text-transform: uppercase; cursor: pointer;
    border-bottom: 2px solid transparent; transition: color 140ms;
  }
  nav button:hover { color: oklch(96% 0.01 70); }
  nav button[aria-current="page"] { color: oklch(98% 0.01 70); border-bottom-color: var(--accent); }
  nav button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  main { padding: clamp(20px, 3vw, 44px) clamp(16px, 3vw, 40px) 80px; max-width: 1560px; }
  /* ── typography ── */
  h1 {
    font-size: clamp(22px, 2.4vw, 31px); font-weight: 600;
    letter-spacing: -0.015em; margin: 0 0 calc(var(--step) * 1);
  }
  .lede { color: var(--ink-soft); margin: 0 0 calc(var(--step) * 8); max-width: 62ch; font-size: 14px; }
  h2 {
    font-size: 12px; font-weight: 700; letter-spacing: 0.13em;
    text-transform: uppercase; color: var(--ink-faint);
    margin: calc(var(--step) * 10) 0 calc(var(--step) * 3);
    padding-bottom: calc(var(--step) * 1.5); border-bottom: 1px solid var(--line);
  }
  h2:first-of-type { margin-top: 0; }
  /* ── readouts: a strip of instrument values, not a grid of cards ── */
  .readout {
    display: flex; flex-wrap: wrap; gap: 0;
    border: 1px solid var(--line-firm); background: var(--surface);
    margin-bottom: calc(var(--step) * 4);
  }
  .readout > div {
    padding: calc(var(--step) * 3.5) calc(var(--step) * 6) calc(var(--step) * 3);
    border-right: 1px solid var(--line); flex: 1 1 auto; min-width: 128px;
  }
  .readout > div:last-child { border-right: 0; }
  .readout dt {
    font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase;
    color: var(--ink-faint); margin-bottom: calc(var(--step) * 1);
  }
  .readout dd {
    margin: 0; font-size: clamp(24px, 2.6vw, 33px); font-weight: 600;
    letter-spacing: -0.02em; line-height: 1.05;
  }
  .readout dd small { font-size: 13px; font-weight: 400; color: var(--ink-faint); letter-spacing: 0; }
  /* ── tables ── */
  .scroll { overflow-x: auto; border: 1px solid var(--line-firm); background: var(--surface); }
  table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
  thead th {
    text-align: left; font-size: 11px; font-weight: 700; letter-spacing: 0.09em;
    text-transform: uppercase; color: var(--ink-faint); white-space: nowrap;
    padding: calc(var(--step) * 2.5) calc(var(--step) * 3);
    background: var(--sunk); border-bottom: 1px solid var(--line-firm);
    position: sticky; top: 0;
  }
  tbody td {
    padding: calc(var(--step) * 2.5) calc(var(--step) * 3);
    border-bottom: 1px solid var(--line); vertical-align: baseline;
  }
  tbody tr:last-child td { border-bottom: 0; }
  tbody tr:hover { background: var(--accent-soft); }
  td.id { font-family: var(--data); font-size: 12.5px; font-weight: 500; overflow-wrap: anywhere; }
  td.num { text-align: right; font-family: var(--data); font-size: 12.5px; }
  td.cmd {
    font-family: var(--data); font-size: 12px; color: var(--ink-soft);
    max-width: 30ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  /* ── status: a solid mark plus a word, no candy badges ── */
  .st { display: inline-flex; align-items: center; gap: calc(var(--step) * 1.5); white-space: nowrap; }
  .st::before {
    content: ""; width: 7px; height: 7px; border-radius: 50%;
    background: var(--idle); flex: none;
  }
  .st.ok::before { background: var(--ok); }
  .st.run::before { background: var(--accent); }
  .st.warn::before { background: var(--warn); }
  .st.bad::before { background: var(--bad); }
  .st.off::before { background: none; box-shadow: inset 0 0 0 1.5px var(--idle); }
  /* ── controls ── */
  .filters {
    display: flex; gap: calc(var(--step) * 2); flex-wrap: wrap;
    margin-bottom: calc(var(--step) * 3); align-items: center;
  }
  input, select {
    font: inherit; font-size: 13px; padding: calc(var(--step) * 1.75) calc(var(--step) * 2.5);
    border: 1px solid var(--line-firm); background: var(--surface); color: var(--ink);
    border-radius: 0;
  }
  input:focus-visible, select:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
  input::placeholder { color: var(--ink-faint); }
  button.act {
    font: inherit; font-size: 12px; letter-spacing: 0.04em;
    padding: calc(var(--step) * 1.5) calc(var(--step) * 2.5);
    background: none; color: var(--ink-soft); cursor: pointer;
    border: 1px solid var(--line-firm); border-radius: 0;
    transition: color 140ms, border-color 140ms;
  }
  button.act:hover { color: var(--bad); border-color: var(--bad); }
  button.act:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button.act[disabled] { opacity: 0.4; cursor: not-allowed; }
  button.act.armed {
    color: var(--bad); border-color: var(--bad); font-weight: 600;
    background: color-mix(in srgb, var(--bad) 8%, transparent);
  }
  button.page {
    font: inherit; font-size: 12px; padding: calc(var(--step) * 1.5) calc(var(--step) * 3);
    border: 1px solid var(--line-firm); background: var(--surface);
    color: var(--ink); cursor: pointer;
  }
  button.page[disabled] { opacity: 0.35; cursor: not-allowed; }
  .pager {
    display: flex; align-items: center; gap: calc(var(--step) * 3);
    margin-top: calc(var(--step) * 3); font-size: 12.5px; color: var(--ink-faint);
  }
  /* ── notices ── */
  .note {
    border-left: 3px solid var(--accent); background: var(--accent-soft);
    padding: calc(var(--step) * 3) calc(var(--step) * 4);
    margin-bottom: calc(var(--step) * 4); font-size: 13.5px;
  }
  .note.bad { border-left-color: var(--bad); }
  .empty { padding: calc(var(--step) * 12) calc(var(--step) * 4); text-align: center; color: var(--ink-faint); }
  .output { border: 1px solid var(--line); margin-bottom: calc(var(--step) * 4); }
  .output header {
    display: flex; flex-wrap: wrap; gap: calc(var(--step) * 3); align-items: baseline;
    padding: calc(var(--step) * 3) calc(var(--step) * 4); border-bottom: 1px solid var(--line);
    font-size: 13px;
  }
  .output header .id { font-family: var(--data); font-size: 12px; }
  .output h3 {
    margin: 0; padding: calc(var(--step) * 2) calc(var(--step) * 4) 0;
    font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint);
  }
  .output pre {
    margin: 0; padding: calc(var(--step) * 2) calc(var(--step) * 3) calc(var(--step) * 3);
    font-family: var(--data); font-size: 12px; white-space: pre-wrap; word-break: break-word;
    max-height: 40vh; overflow: auto;
  }
  .output .none { padding: calc(var(--step) * 2) calc(var(--step) * 4); color: var(--ink-faint); font-size: 12.5px; }
  .empty p { margin: 0 0 calc(var(--step) * 2); }
  .empty code {
    font-family: var(--data); font-size: 12.5px; background: var(--sunk);
    padding: 2px 6px; border: 1px solid var(--line);
  }
  /* ── token gate ── */
  .gate { max-width: 440px; margin: clamp(48px, 12vh, 140px) auto; padding: 0 20px; }
  .gate h1 { font-size: 24px; }
  .gate p { color: var(--ink-soft); font-size: 14px; margin: 0 0 calc(var(--step) * 6); }
  .gate form { display: flex; flex-direction: column; gap: calc(var(--step) * 3); }
  .gate input { padding: calc(var(--step) * 3); font-family: var(--data); font-size: 13px; }
  .gate button {
    font: inherit; font-size: 13px; letter-spacing: 0.06em; text-transform: uppercase;
    padding: calc(var(--step) * 3); border: 0; background: var(--accent);
    color: oklch(99% 0.01 70); cursor: pointer;
  }
  .gate button:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
  .hint { font-family: var(--data); font-size: 12px; color: var(--ink-faint); overflow-wrap: anywhere; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<div id="root"></div>
<script>
"use strict";
// Session storage, not local: an operator token should not outlive the tab it
// was pasted into, and this page is often opened on a shared machine.
const KEY = "agent-sandbox-console-token";
const S = {
  token: sessionStorage.getItem(KEY) || "",
  view: "overview",
  data: null,
  error: "",
  busy: false,
  filters: { status: "", search: "", sandbox_id: "" },
  // Which destructive action is armed and waiting for a second click. Not a
  // `window.confirm`: a host dialog is unavailable in an embedded web view, and
  // where it is unavailable it returns false without showing anything — so the
  // button does nothing and says nothing. It also blocks this page's script, and
  // the live view stops while it is up.
  armed: "",
  page: { limit: 50, offset: 0 },
  // The one execution whose output is open, and its row, so the button can say
  // whether it is showing. Fetched on demand: a page of executions is fifty
  // commands, and inlining their output is what makes such a page unreadable.
  exec: null,
  execKey: "",
};

const el = (tag, attrs, ...kids) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  // Deep flatten: a view returns an array, and a conditional section inside it
  // returns another array, so a single-level flatten would stringify the inner
  // one into "[object HTMLHeadingElement]".
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
};

async function api(path, options) {
  const res = await fetch(path, {
    ...options,
    headers: { Authorization: "Bearer " + S.token, "Content-Type": "application/json" },
  });
  if (res.status === 401) {
    sessionStorage.removeItem(KEY);
    S.token = "";
    throw new Error("Token rejected. Check SANDBOX_INTERNAL_TOKEN.");
  }
  if (!res.ok) {
    // Error bodies are plain text for RuntimeError codes and JSON for
    // HTTPException, so try both rather than showing "[object Object]".
    const body = await res.text();
    let detail = body;
    try { const parsed = JSON.parse(body); detail = parsed.detail || body; } catch (_) {}
    if (typeof detail === "object") detail = JSON.stringify(detail);
    throw new Error(detail || res.status + " " + res.statusText);
  }
  return res.status === 204 ? null : res.json();
}

// ── formatting ──
const ago = (iso) => {
  if (!iso) return "—";
  const secs = (Date.now() - new Date(iso).getTime()) / 1000;
  if (!Number.isFinite(secs)) return "—";
  return dur(secs);
};
const dur = (secs) => {
  if (secs === null || secs === undefined || !Number.isFinite(secs)) return "—";
  if (secs < 1) return "just now";
  if (secs < 60) return Math.floor(secs) + "s";
  if (secs < 3600) return Math.floor(secs / 60) + "m";
  if (secs < 86400) return Math.floor(secs / 3600) + "h " + Math.floor((secs % 3600) / 60) + "m";
  return Math.floor(secs / 86400) + "d " + Math.floor((secs % 86400) / 3600) + "h";
};
const ms = (value) =>
  value === null || value === undefined ? "—" : value < 1000 ? value + "ms" : (value / 1000).toFixed(1) + "s";
const bytes = (value) => {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(value), i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
};
const STATUS_TONE = {
  READY: "ok", ACTIVE: "ok", RUNNING: "run", ASSIGNED: "warn",
  RELEASING: "warn", RELEASED: "off", SUCCEEDED: "ok",
  FAILED: "bad", TIMED_OUT: "bad", CANCELLED: "off", LOST: "bad", DRAINING: "warn",
};
const status = (value) => el("span", { class: "st " + (STATUS_TONE[value] || "") }, value || "—");

// ── views ──
function gate() {
  const input = el("input", {
    type: "password", placeholder: "SANDBOX_INTERNAL_TOKEN",
    autocomplete: "off", spellcheck: "false", required: true, id: "tok",
  });
  return el("div", { class: "gate" },
    el("h1", {}, "Sandloom fleet console"),
    el("p", {}, "Paste the internal token this service was started with. It is kept in this tab only."),
    S.error ? el("div", { class: "note bad" }, S.error) : null,
    el("form", {
      onsubmit: (event) => {
        event.preventDefault();
        if (!input.value.trim()) return;
        S.token = input.value.trim();
        sessionStorage.setItem(KEY, S.token);
        S.error = "";
        load();
      },
    }, el("label", { for: "tok" }, "Internal token"), input,
       el("button", { type: "submit" }, "Connect")),
    el("p", { class: "hint", style: "margin-top:24px" }, "echo $SANDBOX_INTERNAL_TOKEN"),
  );
}

function overview(d) {
  const iso = d.isolation || {};
  const disk = d.disk || {};
  const rec = d.reclamation || {};
  const byStatus = d.sandboxes_by_status || {};
  const order = ["READY", "RUNNING", "ASSIGNED", "RELEASING", "RELEASED"];
  const seen = Object.keys(byStatus);
  const statuses = order.filter((k) => k in byStatus).concat(seen.filter((k) => !order.includes(k)));
  return [
    el("h1", {}, "Fleet"),
    el("p", { class: "lede" },
      "Live capacity comes from worker heartbeats; sandbox counts come from the control-plane database."),
    !d.fleet_queries_available
      ? el("div", { class: "note" },
          "This metadata backend cannot run fleet-wide queries, so sandbox and execution " +
          "listings are unavailable. Capacity below is read from the registry.")
      : null,
    el("h2", {}, "Capacity"),
    el("dl", { class: "readout" },
      el("div", {}, el("dt", {}, "Sandboxes"), el("dd", {}, d.sandbox_total)),
      el("div", {}, el("dt", {}, "Running now"),
        el("dd", {}, d.running_sessions_total, el("small", {}, " / " + d.capacity_total))),
      el("div", {}, el("dt", {}, "Workers live"),
        el("dd", {}, d.live_worker_total, el("small", {}, " / " + d.worker_total))),
      el("div", {}, el("dt", {}, "Templates"),
        el("dd", {}, d.template_total,
          el("small", {}, d.templates_shared ? " fleet-wide" : " this worker only"))),
      el("div", {}, el("dt", {}, "Isolation"),
        el("dd", { style: "font-size:20px" }, iso.selected_level || "unknown")),
      el("div", {}, el("dt", {}, "Disk free"),
        el("dd", { style: "font-size:20px" }, bytes(disk.free_bytes),
          el("small", {}, disk.used_percent === undefined ? "" : "  " + disk.used_percent + "% used"))),
      // The question an operator asks while looking at an idle sandbox that
      // nobody has reclaimed: when does the sweep run, and how long is idle?
      // One that has been shortened for a test says so by showing the numbers
      // it is running with rather than the shipped ones.
      el("div", {}, el("dt", {}, "Reclamation"),
        el("dd", {}, rec.idle_ttl_seconds === undefined ? "unknown" : dur(rec.idle_ttl_seconds),
          el("small", {}, rec.maintenance_interval_seconds === undefined ? "" :
            "  idle · sweep " + dur(rec.maintenance_interval_seconds) +
            " · grace " + dur(rec.orphan_release_grace_seconds)))),
    ),
    statuses.length
      ? [el("h2", {}, "Sandboxes by status"),
         el("dl", { class: "readout" },
           statuses.map((key) => el("div", {},
             el("dt", {}, key.toLowerCase()), el("dd", {}, byStatus[key]))))]
      : null,
    el("h2", {}, "Workers"),
    d.workers && d.workers.length
      ? el("div", { class: "scroll" }, el("table", {},
          el("thead", {}, el("tr", {},
            ["Worker", "Profile", "State", "Heartbeat", "Sessions", "Capacity", "Endpoint", "Epoch", "Uptime"]
              .map((h) => el("th", {}, h)))),
          el("tbody", {}, d.workers.map((w) => el("tr", {},
            el("td", { class: "id" }, w.worker_id),
            // The whole hash, not a prefix: `bubblewrap-0.11.2-generic-v12-basic-net-host`
            // and `...-other-basic-net-host` differ in the middle, and a fleet that
            // is half rebuilt is exactly the fleet where two of these rows have to
            // be comparable at a glance. A worker only accepts a sandbox whose
            // profile matches its own, so this column is the one that explains
            // "no worker available" in a fleet that looks healthy.
            el("td", { class: "id", title: w.profile_hash }, w.profile_hash || "—"),
            el("td", {}, el("span", { class: "st " + (w.live ? "ok" : "bad") }, w.live ? w.status : "no heartbeat")),
            el("td", { class: "num" }, dur(w.heartbeat_age_seconds)),
            el("td", { class: "num" }, w.running_sessions),
            el("td", { class: "num" }, w.capacity),
            el("td", { class: "id" }, w.endpoint),
            el("td", { class: "id" }, (w.worker_epoch || "").slice(0, 8)),
            el("td", { class: "num" }, ago(w.started_at)),
          )))))
      : el("div", { class: "scroll" }, el("div", { class: "empty" },
          el("p", {}, "No worker rows. The service registers itself on startup, so this is " +
                      "either a fresh database or a registry the console cannot read."))),
    iso.probe_failures && Object.keys(iso.probe_failures).length
      ? [el("h2", {}, "Isolation probe failures"),
         el("div", { class: "scroll" }, el("table", {},
           el("thead", {}, el("tr", {}, el("th", {}, "Level"), el("th", {}, "Why it is unavailable"))),
           el("tbody", {}, Object.entries(iso.probe_failures).map(([level, why]) =>
             el("tr", {}, el("td", { class: "id", style: "white-space:nowrap" }, level), el("td", { class: "cmd", style: "max-width:none;white-space:normal" }, why))))))]
      : null,
    el("h2", {}, "Effective isolation capabilities"),
    el("p", { class: "hint" },
      (iso.profile_mode === "custom" ? "Custom additive profile on " : "Level profile: ") +
      (iso.selected_level || "unknown") + ". Network and resource quotas are separate policies."),
    el("p", { class: "hint" }, (iso.features || []).join(" · ") || "No capability report"),
    iso.feature_policy && Object.keys(iso.feature_policy.skipped_optional || {}).length
      ? el("div", { class: "note" }, "Optional capabilities skipped: " +
          Object.entries(iso.feature_policy.skipped_optional).map(([name, why]) => name + ": " + why).join("; "))
      : null,
    iso.toolchains && iso.toolchains.checks
      ? [el("h2", {}, "Toolchain launch checks"),
         el("div", { class: "scroll" }, el("table", {},
           el("thead", {}, el("tr", {},
             ["Toolchain", "Result", "Detail"].map((h) => el("th", {}, h)))),
           el("tbody", {}, Object.entries(iso.toolchains.checks).map(([name, check]) =>
             el("tr", {}, el("td", { class: "id" }, name),
               el("td", {}, el("span", { class: "st " + (check.status === "available" ? "ok" : "warn") }, check.status)),
               el("td", { class: "cmd", style: "max-width:none;white-space:normal" }, check.detail || "—"))))))]
      : null,
  ];
}

function sandboxes(d) {
  const rows = d.sandboxes || [];
  return [
    el("h1", {}, "Sandboxes"),
    el("p", { class: "lede" }, "Every sandbox the control plane knows about, newest activity first."),
    el("div", { class: "filters" },
      el("input", {
        type: "search", placeholder: "Search sandbox id", value: S.filters.search,
        oninput: (e) => { S.filters.search = e.target.value; debounce(); },
      }),
      el("select", {
        onchange: (e) => { S.filters.status = e.target.value; S.page.offset = 0; load(); },
      }, ["", "READY", "RUNNING", "ASSIGNED", "RELEASING", "RELEASED"].map((v) =>
        el("option", { value: v, selected: S.filters.status === v }, v || "All statuses"))),
    ),
    rows.length
      ? el("div", { class: "scroll" }, el("table", {},
          el("thead", {}, el("tr", {},
            ["Sandbox", "Status", "Scope", "Worker", "Gen", "UID", "Idle", "Lifetimes", "Store", ""]
              .map((h) => el("th", {}, h)))),
          el("tbody", {}, rows.map((s) => el("tr", {},
            el("td", { class: "id" }, s.sandbox_id),
            el("td", {}, status(s.status)),
            el("td", {}, s.workspace_scope_id),
            el("td", { class: "id" }, s.worker_id || "—"),
            el("td", { class: "num" }, s.generation),
            el("td", { class: "num" }, s.sandbox_uid),
            el("td", { class: "num" }, dur(s.idle_seconds)),
            el("td", { class: "num" }, s.lifecycle_count),
            el("td", {}, s.storage_mode),
            el("td", {}, s.status === "RELEASED" ? null : el("button", {
              class: S.armed === "release:" + s.sandbox_id ? "act armed" : "act",
              disabled: S.busy,
              title: "Deletes the workspace. The sandbox is recreated at a new "
                + "generation on the next resolve, and the files are gone.",
              onclick: () => release(s.sandbox_id),
            }, S.armed === "release:" + s.sandbox_id ? "Confirm release" : "Release")),
          )))))
      : el("div", { class: "scroll" }, el("div", { class: "empty" },
          el("p", {}, S.filters.search || S.filters.status
            ? "No sandbox matches this filter."
            : "No sandboxes yet. Create one to see it here:"),
          S.filters.search || S.filters.status ? null
            : el("code", {}, "POST /api/v1/sandboxes/resolve"))),
    pager(d),
  ];
}

function executionDetail() {
  const x = S.exec;
  if (!x) return null;
  return el("section", { class: "output" },
    el("header", {},
      el("span", { class: "id" }, x.exec_id),
      status(x.status),
      el("span", {}, "exit ", el("b", {}, x.exit_code === null || x.exit_code === undefined ? "—" : x.exit_code)),
      el("span", {}, ms(x.duration_ms)),
      el("span", { class: "id" }, x.sandbox_id),
      el("button", { class: "act", onclick: () => { S.exec = null; S.execKey = ""; render(); } }, "Close"),
    ),
    el("h3", {}, "Command"),
    el("pre", {}, (x.argv || []).join(" ") || "—"),
    el("h3", {}, "stdout"),
    x.stdout ? el("pre", {}, x.stdout) : el("div", { class: "none" }, "nothing on stdout"),
    el("h3", {}, "stderr"),
    x.stderr ? el("pre", {}, x.stderr) : el("div", { class: "none" }, "nothing on stderr"),
    x.truncated ? el("div", { class: "note" },
      "This execution's output was truncated at the configured limit, so what is " +
      "above is the beginning of it.") : null,
  );
}

// Execution IDs are unique within a sandbox, not across the fleet. JSON
// encoding the pair also avoids delimiter collisions in user-chosen IDs.
const executionKey = (x) => JSON.stringify([x.sandbox_id, x.exec_id]);

async function openExecution(x) {
  const key = executionKey(x);
  if (S.execKey === key) { S.exec = null; S.execKey = ""; render(); return; }
  // The listing deliberately carries no output; this asks for the one
  // execution the operator opened. The admin route rather than the sandbox
  // route, because the sandbox is usually gone by the time anyone reads its
  // failures: that route answers for a live sandbox and refuses a released one.
  const path = "/api/v1/admin/execs/" + encodeURIComponent(x.sandbox_id) +
    "/" + encodeURIComponent(x.exec_id);
  try {
    const detail = await api(path);
    S.exec = detail;
    S.execKey = key;
    S.error = "";
  } catch (err) {
    S.error = err.message;
    S.exec = null;
    S.execKey = "";
  }
  render();
}

function execs(d) {
  const rows = d.execs || [];
  return [
    el("h1", {}, "Executions"),
    el("p", { class: "lede" },
      "Recorded commands, newest first. The list carries no output; Read opens one."),
    el("div", { class: "filters" },
      el("input", {
        type: "search", placeholder: "Filter by exact sandbox id", value: S.filters.sandbox_id,
        oninput: (e) => { S.filters.sandbox_id = e.target.value; debounce(); },
      }),
      el("select", {
        onchange: (e) => { S.filters.status = e.target.value; S.page.offset = 0; load(); },
      }, ["", "RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"].map((v) =>
        el("option", { value: v, selected: S.filters.status === v }, v || "All statuses"))),
    ),
    executionDetail(),
    rows.length
      ? el("div", { class: "scroll" }, el("table", {},
          el("thead", {}, el("tr", {},
            ["Exec", "Sandbox", "Status", "Exit", "Command", "Scope", "Duration", "Started", "Output"]
              .map((h) => el("th", {}, h)))),
          el("tbody", {}, rows.map((x) => el("tr", {},
            el("td", { class: "id" }, x.exec_id),
            el("td", { class: "id" }, x.sandbox_id),
            el("td", {}, status(x.status)),
            el("td", { class: "num" }, x.exit_code === null || x.exit_code === undefined ? "—" : x.exit_code),
            el("td", { class: "cmd", title: (x.argv || []).join(" ") }, (x.argv || []).join(" ") || "—"),
            el("td", {}, x.exec_scope || el("span", { style: "color:var(--ink-faint)" }, "exclusive")),
            el("td", { class: "num" }, ms(x.duration_ms)),
            el("td", { class: "num" }, ago(x.started_at || x.created_at)),
            el("td", {}, el("button", {
              class: "act" + (S.execKey === executionKey(x) ? " armed" : ""),
              onclick: () => openExecution(x),
            }, S.execKey === executionKey(x) ? "Hide" : "Read")),
          )))))
      : el("div", { class: "scroll" }, el("div", { class: "empty" },
          el("p", {}, "No executions recorded."))),
    pager(d),
  ];
}

function templates(d) {
  const rows = d.templates || [];
  return [
    el("h1", {}, "Environment templates"),
    el("p", { class: "lede" },
      "Prebuilt environment trees, mounted read-only into any sandbox that attaches them."),
    rows.length
      ? el("div", { class: "scroll" }, el("table", {},
          el("thead", {}, el("tr", {},
            ["Name", "Digest", "Size", "Mounts at", "Built from", "Description", ""]
              .map((h) => el("th", {}, h)))),
          el("tbody", {}, rows.map((t) => el("tr", {},
            el("td", { class: "id" }, t.name),
            el("td", { class: "id", title: t.digest }, (t.digest || "").replace("sha256:", "").slice(0, 12)),
            el("td", { class: "num" }, bytes(t.size_bytes)),
            el("td", { class: "id" }, t.mount_target),
            el("td", { class: "id" }, t.source_sandbox_id || "—"),
            el("td", {}, t.description || "—"),
            el("td", {}, el("button", {
              class: S.armed === "unpublish:" + t.name ? "act armed" : "act",
              disabled: S.busy,
              title: "Removes the name for every worker. Sandboxes already "
                + "mounting it keep running, and the stored objects are kept.",
              onclick: () => unpublish(t.name),
            }, S.armed === "unpublish:" + t.name ? "Confirm unpublish" : "Unpublish")),
          )))))
      : el("div", { class: "scroll" }, el("div", { class: "empty" },
          el("p", {}, "No templates published. Build one from a sandbox that has the environment ready:"),
          el("code", {}, "POST /api/v1/sandboxes/{id}/templates"))),
  ];
}

function pager(d) {
  if (!d || d.total === undefined) return null;
  const from = d.total === 0 ? 0 : d.offset + 1;
  const to = Math.min(d.offset + d.limit, d.total);
  return el("div", { class: "pager" },
    el("button", {
      class: "page", disabled: d.offset === 0,
      onclick: () => { S.page.offset = Math.max(0, d.offset - d.limit); load(); },
    }, "← Newer"),
    el("span", {}, from + "–" + to + " of " + d.total),
    el("button", {
      class: "page", disabled: to >= d.total,
      onclick: () => { S.page.offset = d.offset + d.limit; load(); },
    }, "Older →"),
  );
}

// ── actions ──
//
// Both of these destroy something that does not come back — a workspace, or a
// name every worker resolves — so both ask first. The asking happens in the page
// rather than through `window.confirm`, which an embedded web view answers with
// `false` without showing anything, leaving a button that silently does nothing.
// Here the button says "Confirm" and the second click is the one that acts.
function arm(key) {
  S.armed = key;
  render();
  // A click that is never followed up should not leave a live confirmation
  // behind, and fifteen seconds is also the poll interval: by then the operator
  // is looking at something else.
  setTimeout(() => {
    if (S.armed === key) { S.armed = ""; render(); }
  }, 15000);
}

async function act(key, run) {
  if (S.armed !== key) { arm(key); return; }
  S.armed = "";
  S.busy = true; render();
  try {
    await run();
  } catch (err) {
    S.error = err.message;
  } finally {
    S.busy = false;
    load();
  }
}

async function release(id) {
  // Release is reversible — the next resolve recreates the sandbox at a new
  // generation — but the workspace it deletes is not, which is why this is one
  // of the two that asks.
  await act("release:" + id, () =>
    api("/api/v1/sandboxes/" + encodeURIComponent(id), { method: "DELETE" }));
}

async function unpublish(name) {
  await act("unpublish:" + name, () =>
    api("/api/v1/templates/" + encodeURIComponent(name), { method: "DELETE" }));
}

// ── plumbing ──
let timer = null;
function debounce() {
  clearTimeout(timer);
  timer = setTimeout(() => { S.page.offset = 0; load(); }, 250);
}

function endpoint() {
  const q = new URLSearchParams();
  if (S.view === "sandboxes") {
    if (S.filters.status) q.set("status", S.filters.status);
    if (S.filters.search) q.set("search", S.filters.search);
    q.set("limit", S.page.limit); q.set("offset", S.page.offset);
    return "/api/v1/admin/sandboxes?" + q;
  }
  if (S.view === "execs") {
    if (S.filters.status) q.set("status", S.filters.status);
    if (S.filters.sandbox_id) q.set("sandbox_id", S.filters.sandbox_id);
    q.set("limit", S.page.limit); q.set("offset", S.page.offset);
    return "/api/v1/admin/execs?" + q;
  }
  if (S.view === "templates") return "/api/v1/templates";
  return "/api/v1/admin/overview";
}

async function load() {
  if (!S.token) { render(); return; }
  // What is on screen right now, or null when an error is showing: an error
  // must be cleared by a successful load even if the data is unchanged.
  const shown = S.error ? null : JSON.stringify(S.data);
  try {
    const fresh = await api(endpoint());
    // Re-rendering rebuilds every node, including the ones the operator is
    // pointing at, so a quiet fleet is redrawn for nothing and a click can land
    // on a button that has already been replaced. The poll only needs to redraw
    // when something actually changed; actions call this directly and changed
    // state, so they still redraw.
    if (shown !== null && JSON.stringify(fresh) === shown) return;
    S.data = fresh;
    S.error = "";
  } catch (err) {
    S.error = err.message;
    S.data = null;
  }
  render();
}

function go(view) {
  if (S.view === view) return;
  // An armed button belongs to the view it was armed on; `load()` below redraws,
  // so this only has to clear the flag.
  S.armed = "";
  S.view = view;
  // Filters mean different things per view, so carrying them across would
  // silently apply a sandbox status to an execution query.
  S.filters = { status: "", search: "", sandbox_id: "" };
  S.page.offset = 0;
  S.data = null;
  load();
}

const VIEWS = {
  overview: { label: "Fleet", render: overview },
  sandboxes: { label: "Sandboxes", render: sandboxes },
  execs: { label: "Executions", render: execs },
  templates: { label: "Templates", render: templates },
};

function render() {
  const root = document.getElementById("root");
  root.textContent = "";
  if (!S.token) { root.append(gate()); return; }
  root.append(
    el("header", { class: "bar" },
      el("span", { class: "mark" }, el("b", {}, "Sandloom")),
      el("nav", {}, Object.entries(VIEWS).map(([key, view]) =>
        el("button", {
          onclick: () => go(key),
          "aria-current": S.view === key ? "page" : null,
        }, view.label))),
    ),
    el("main", {},
      S.error ? el("div", { class: "note bad" }, S.error) : null,
      S.data ? VIEWS[S.view].render(S.data)
             : !S.error ? el("p", { class: "lede" }, "Loading…") : null,
    ),
  );
}

// A console left open should not show a frozen fleet, but reloading while an
// operator is typing a filter or reading a row would be worse than stale data.
setInterval(() => {
  if (S.token && !S.busy && S.view === "overview" && !document.hidden) load();
}, 15000);

load();
</script>
</body>
</html>
"""


@lru_cache(maxsize=1)
def console_html() -> str:
    """Return the console page.

    Cached because the document is a constant and is re-served on every hit.
    """
    return _CONSOLE_HTML


__all__ = ["console_html"]
