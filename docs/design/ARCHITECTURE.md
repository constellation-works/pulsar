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

An arrow is "may import". Dependencies point down, and the directory tree shows it: a
module imports only what sits beneath it, never above or beside it, and no import climbs
the tree with `from ..` (`core/ledger` is the one known exception). Among the front ends,
`cli` starts the other two (`serve`, `orbit-tool`), so it ranks above them.

Dependencies are supplied from the top. `main.py` is the one place that constructs: it
builds `App` (the verbs, bound to one home and one transport) and hands it to the front
end, which calls it and never builds anything below. Tests hand in an `App` on the fake
transport the same way.

| Rank | Package | Owns | Also must not import |
|---|---|---|---|
| 0 | `core` | plans, the publisher, the ledger, policy, accounts and credential storage, media loading, the secret scanner, settings | `httpx`, `mcp` |
| 1 | `providers/<name>` | one channel: its HTTP client, OAuth, and the `Channel` adapter | `mcp` |
| 2 | `app` | the `Runtime` that joins settings, storage, the ledger and providers; the reports and operator verbs, as functions returning `(report, exit_code)` | `mcp` |
| 3 | `mcp.py`, `orbit_tool.py` | the MCP server and the Orbit exec backend | |
| 4 | `cli/` | the one dispatcher: parses argv, prints, maps errors to exit codes | |
| 5 | `main.py` | the entry point: builds `App` and runs the CLI with it; nothing imports it | |

Inside `app`, `runtime.py` (rank 0) comes before `health.py` and `ops.py` (rank 1), and
`facade.py` (`App`, rank 2) sits on them.

A layer is used only through its public API: its package's `__init__.py`. Code outside
the package imports from the package root (`from pulsar.core import Ledger`), and only the
names its `__all__` lists; modules inside the package import each other directly. Adding
to `__all__` is the deliberate act of widening the layer. `core`, `providers/x`,
`app` and `cli` are held to it. The front ends and `main` go through `app` only: what they
need from `core` or a provider, `app` re-exports.

Inside `cli/`, `main.py` (`run`) parses and dispatches; everything it dispatches to sits
beneath it in `commands/`, and `main.py` uses only what `commands/__init__.py` exports.
Inside `commands/`:

| Rank | Module | Role |
|---|---|---|
| 0 | `render.py`, `errors.py` | output modes and rendering; failure printing and exit codes |
| 1 | `views.py`, `parser.py` | each payload's human view; the argparse pieces commands declare with |
| 2 | `context.py` | what a handler gets (`Context`, carrying the supplied `App`) and how it prints (`emit`, `notice`) |
| 3 | `auth.py`, `status.py`, `history.py`, `publish.py`, `reconcile.py`, `maintenance.py`, `services.py` | one module per command (or per help group): its parser and handler |

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
