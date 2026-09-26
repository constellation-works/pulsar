---
title: Architecture
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
summary: The layers of pulsar, which way imports point, and where ambient state (environment, cwd, home) is read.
---

# Architecture

pulsar is one core with three front ends (the surfaces). This page describes its layers and
where ambient state is read.

## Layers

```
entry        main                            builds the App, supplies it to a front end
                 │
front ends   cli ─▶ mcp, orbit_tool          the surfaces: argv, MCP, the Orbit envelope
                 │
app          App ─▶ ops, health ─▶ runtime   the verbs every front end calls; composition
                 │
providers    x (client, auth, adapter)       one channel each
                 │
core         plan, publisher, ledger, policy, accounts, store, media, guard, settings, paths
```

An arrow points at what a layer imports. The CLI starts the other two front ends (`serve`,
`orbit-tool`).

`main.py` builds `App` (the verbs, bound to one home and one transport) and hands it to the
CLI. Tests hand in an `App` on the fake transport the same way.

| Layer | Owns |
|---|---|
| `core` | plans, the publisher, the ledger, policy, accounts and credential storage, media loading, the secret scanner, settings; no HTTP, no MCP |
| `providers/<name>` | one channel: its HTTP client, OAuth, and the `Channel` adapter |
| `app` | `App`, over the `Runtime` that joins settings, storage, the ledger and providers (`runtime.py`), the operator verbs (`ops.py`) and account health (`health.py`), each returning `(report, exit_code)` |
| `mcp.py`, `orbit_tool.py` | the MCP server and the Orbit exec backend |
| `cli/` | the dispatcher: `main.py` (`run`) parses argv and dispatches to `commands/` (one module per command, each declaring itself in `register`), which print and fail through `toolkit/` (`parser`, `context`, `render`, `views`, `errors`) |
| `main.py` | the entry point: builds `App` and runs the CLI with it |

## Ambient state

`core` takes the environment, the working directory and the home as arguments.
The front ends and `app` resolve them once, at the edge:

- **Home.** `PULSAR_HOME`, else `~/.config/pulsar`; under Orbit, `$ORBIT_PLUGIN_STATE/home`
  (`orbit_tool.plugin_paths`). `app.runtime.default_paths` is where a front end turns the
  environment into `Paths`; no core module reads the environment, the cwd or `$HOME`.
- **Relative media paths** start from the `media_base` a surface passes to `Runtime`: the
  process cwd for the CLI and MCP server, the workspace root for the Orbit backend. Core
  refuses a relative path with no base.
- **Caller label.** `Runtime.caller` reads `PULSAR_CALLER` from the environment it was given.

## Errors

Every failure a caller can act on is a `PulsarError` with a stable `code`
([error codes](surfaces/references/error-codes.md)). Each surface maps it once: the MCP server
to `{ok: false, code, …}`, the Orbit backend to its envelope, the CLI to JSON on stderr and
exit 1. An exception that is not a `PulsarError` is a bug, reported as `internal`.
