---
title: Architecture
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
summary: The layers of pulsar, which way imports point, and where ambient state (environment, cwd, home) is read.
---

# Architecture

pulsar is one core with three front ends (the surfaces). This page fixes the layer order (STD-02 §R1);
[tests/test_layering.py](../../tests/test_layering.py) checks it on the import graph.

## Layers

```
front ends   cli ─▶ mcp, orbit_tool          the surfaces: argv, MCP, the Orbit envelope
                 │
app          ops, health ─▶ runtime          the verbs every front end calls; composition
                 │
providers    x (client, auth, adapter)       one channel each
                 │
core         plan, publisher, ledger, policy, accounts, store, media, guard, settings, paths
```

An arrow is "may import". A layer imports only the layers below it, never above or
beside it; the one exception is that `cli` starts the other two front ends (`serve`,
`orbit-tool`), so it ranks above them and nothing imports `cli`.

| Rank | Package | Owns | Also must not import |
|---|---|---|---|
| 0 | `core` | plans, the publisher, the ledger, policy, accounts and credential storage, media loading, the secret scanner, settings | `httpx`, `mcp` |
| 1 | `providers/<name>` | one channel: its HTTP client, OAuth, and the `Channel` adapter | `mcp` |
| 2 | `app` | the `Runtime` that joins settings, storage, the ledger and providers; the reports and operator verbs, as functions returning `(report, exit_code)` | `mcp` |
| 3 | `mcp.py`, `orbit_tool.py` | the MCP server and the Orbit exec backend | |
| 4 | `cli/` | the one dispatcher: parses argv, prints, maps errors to exit codes | |

Inside `app`, `runtime.py` (rank 0) comes before `health.py` and `ops.py` (rank 1).

A layer is used only through its public API: its package's `__init__.py`. Code outside
the package imports from the package root (`from pulsar.core import Ledger`), and only the
names its `__all__` lists; modules inside the package import each other directly. Adding
to `__all__` is the deliberate act of widening the layer. `core` and `providers/x`
are held to this today; `app` follows.

Inside `cli/`, the same rule:

| Rank | Module | Role |
|---|---|---|
| 0 | `render.py`, `errors.py` | output modes and rendering; failure printing and exit codes |
| 1 | `views.py`, `parser.py` | each payload's human view; the argparse pieces commands declare with |
| 2 | `context.py` | what a handler gets (`Context`) and how it prints (`emit`, `notice`) |
| 3 | `commands/` | one module per command (or per help group): its parser and handler |
| 4 | `main.py` | builds the tree from `commands/`, resolves the mode once, dispatches |

## Ambient state

`core` takes the environment, the working directory and the home as arguments (STD-02 §R3).
The front ends and `app` resolve them once, at the edge:

- **Home.** `PULSAR_HOME`, else `~/.config/pulsar`; under Orbit, `$ORBIT_PLUGIN_STATE/home`
  (`orbit_tool.plugin_paths`). `app.runtime.default_paths` is where a front end turns the
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
