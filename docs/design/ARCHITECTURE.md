---
title: Architecture
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
summary: The layers of pulsar, which way imports point, and where ambient state (environment, cwd, home) is read.
---

# Architecture

pulsar is one set of packages with three front ends (the surfaces). This page describes its layers and
where ambient state is read.

## Layers

```
entry        main                                   builds the App, supplies it to a front end
                 │
surfaces     cli ─▶ mcp, orbit                      argv, MCP, the Orbit envelope
                 │
app          App ─▶ ops, health ─▶ runtime          the verbs; the front ends' one gateway
                 │
channels.x   client, auth, adapter                  the X channel
                 │
publishing   publisher, policy, media               plan → claimed, sent, settled posts
accounts     registry, store                        aliases, bound identities, credentials
ledger       SqliteLedger, usage                    one row per write, committed before it leaves
channels     contract                               the Channel protocol and its values
home         paths, files, settings                 the home directory and config.toml
plan         model, aliases                         the channel-neutral publish plan
guard, jsonx, errors                                the scanner, JSON views, error codes
```

An arrow points at what a layer imports; each package imports only packages below it, and
none imports its callers. The CLI starts the other two front ends (`serve`, `orbit-tool`).
The front ends import nothing below `app`: what they need from the packages beneath (error
codes, the plan, ledger states, media limits) is re-exported from `pulsar.app`.

Each package is named for what its modules are, and exposes what other packages use in its
`__init__.py`; callers import the package, not its modules. `channels` does not import its
implementations (`channels.x` stands on `accounts`, which stands on the contract).

`main.py` builds a `LocalApp` (the verbs, bound to one home and one transport) and hands it to
the CLI, which types it as `App`, the protocol it codes against. Tests hand in a `LocalApp` on
the fake transport the same way. Every injected dependency is typed by such a protocol
(`App`, `Runtime`, `Ledger`, `CredentialStore`, `Channel`, `XApi`); only the code that builds
it names the implementation (`LocalApp`, `LocalRuntime`, `SqliteLedger`, `FernetFileStore`,
`XChannel`, `XClient`).

| Package | Owns |
|---|---|
| `errors`, `jsonx`, `guard` | the error codes and `PulsarError`; typed views over parsed JSON; the secret scanner and redaction |
| `plan` | the publish plan and its canonical digest (`model.py`); `provider:handle` aliases (`aliases.py`) |
| `home` | the home layout (`paths.py`), owner-only files and locks (`files.py`), `config.toml` (`settings.py`) |
| `channels` | the `Channel` contract (`contract.py`); `channels/x/` is the X channel: its HTTP client, OAuth and the `Channel` adapter |
| `accounts` | the account registry (`registry.py`) and the encrypted credential store (`store.py`) |
| `ledger` | the write ledger, its schema and queries (`SqliteLedger`), and the usage it sums (`usage.py`) |
| `publishing` | the `Publisher` (`publisher.py`), budget, cap and quiet-hour policy (`policy.py`), media loading (`media.py`) |
| `app` | the `App` and `Runtime` protocols (`interfaces.py`); `LocalApp` (`facade.py`), over the `LocalRuntime` that joins settings, storage, the ledger and channels (`runtime.py`), the operator verbs (`ops.py`), account health (`health.py`), the `writes.jsonl` export (`writelog.py`) and the `posted.jsonl` import (`importer.py`), each verb returning `(report, exit_code)` |
| `surfaces/mcp/`, `surfaces/orbit/` | the MCP server (`server.py`) and the Orbit exec backend (`backend.py`) |
| `surfaces/cli/` | the dispatcher: `main.py` (`run`) parses argv and dispatches to `commands/` (one module per command, each declaring itself in `register`), which print and fail through `toolkit/` (`parser`, `context`, `render`, `views`, `errors`) |
| `main.py` | the entry point: reads the environment, cwd and `$HOME`, builds `LocalApp` and runs the CLI with it |

## Ambient state

Nothing below `main` reads the environment, the working directory or the home itself.
The entry point (`pulsar.main`) reads them once and builds the `App` with them; everything
below receives them from there:

- **Home.** `PULSAR_HOME`, else `~/.config/pulsar`; under Orbit, `$ORBIT_PLUGIN_STATE/home`
  (`app.default_paths`). The Orbit backend refuses a `PULSAR_HOME` that names another home
  (`surfaces.orbit.backend.check_plugin_home`). Orbit runs the backend as `pulsar orbit-tool`, so it gets
  the same `App`.
- **Relative media paths** start from the `media_base` `App.runtime` passes to the `Runtime`: the
  process cwd for the CLI and MCP server, the workspace root for the Orbit backend. Media
  loading refuses a relative path with no base.
- **Caller label.** `Runtime.caller` reads `PULSAR_CALLER` from the environment `App` was given.

## Errors

Every failure a caller can act on is a `PulsarError` with a stable `code`
([error codes](surfaces/references/error-codes.md)). Each surface maps it once: the MCP server
to `{ok: false, code, …}`, the Orbit backend to its envelope, the CLI to JSON on stderr and
exit 1. An exception that is not a `PulsarError` is a bug, reported as `internal`.
