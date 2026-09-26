---
title: Architecture
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
summary: The layers of pulsar, which way imports point, and where ambient state (environment, cwd, home) is read.
---

# Architecture

pulsar is one core with three front ends. This page fixes the layer order (STD-02 §R1);
[tests/test_layering.py](../../tests/test_layering.py) checks it on the import graph.

## Layers

```
surfaces   cli ─▶ mcp, orbit_tool ─▶ health, ops ─▶ runtime
              │                                       │
providers  x (client, auth, adapter) ◀────────────────┘
              │
core       plan, publisher, ledger, policy, accounts, store, media, guard, settings, paths
```

An arrow is "may import". Each layer imports only the layers below it:

| Layer | Owns | Must not import |
|---|---|---|
| `core` | plans, the publisher, the ledger, policy, accounts and credential storage, media loading, the secret scanner, settings | `httpx`, `mcp`, `providers`, `surfaces` |
| `providers/<name>` | one channel: its HTTP client, OAuth, and the `Channel` adapter | `mcp`, `surfaces` |
| `surfaces` | composition and the front ends | nothing below restricts it |

Inside `surfaces`, a module imports only modules of a lower rank:

| Rank | Module | Role |
|---|---|---|
| 0 | `runtime.py` | composition: joins settings, storage, the ledger and providers into a `Runtime` |
| 1 | `health.py`, `ops.py` | the reports and operator verbs, as functions returning `(report, exit_code)` |
| 2 | `mcp.py`, `orbit_tool.py` | the MCP server and the Orbit exec backend |
| 3 | `cli.py` | the one dispatcher: parses argv, prints, maps errors to exit codes |

## Ambient state

`core` takes the environment, the working directory and the home as arguments (STD-02 §R3).
The surfaces resolve them once, at the edge:

- **Home.** `PULSAR_HOME`, else `~/.config/pulsar`; under Orbit, `$ORBIT_PLUGIN_STATE/home`
  (`orbit_tool.plugin_paths`). `runtime.default_paths` is where a surface turns the
  environment into `Paths`; no core module reads the environment, the cwd or `$HOME`, and
  `tests/test_layering.py` holds it there.
- **Relative media paths** start from the `media_base` a surface passes to `Runtime`: the
  process cwd for the CLI and MCP server, the workspace root for the Orbit backend. Core
  refuses a relative path with no base.
- **Caller label.** `Runtime.caller` reads `PULSAR_CALLER` from the environment it was given.

## Errors

Every failure a caller can act on is a `PulsarError` with a stable `code`
([error codes](surfaces/references/error-codes.md)). Each surface maps it once: the MCP server
to `{ok: false, code, …}`, the Orbit backend to its envelope, the CLI to JSON on stderr and
exit 1. An exception that is not a `PulsarError` is a bug, reported as `internal`.
