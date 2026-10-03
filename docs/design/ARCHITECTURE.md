---
title: Architecture
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Accepted
summary: The layers of pulsar, which way imports point, and where ambient state (environment, cwd, home) is read.
---

# Architecture

pulsar is one application with three front ends (the surfaces). This page describes its layers and
where ambient state is read.

## Layers

The tree is the architecture: a package sits beneath what uses it, and imports point down.

```
src/pulsar/
├── main.py              the entry point: reads the process once, builds LocalApp, runs the CLI
├── cli/                 the command line (main, commands/, toolkit/); starts mcp and orbit
├── mcp/                 the MCP server: schemas and annotations over app/tools.py
├── orbit/               the Orbit plugin backend: envelopes and sandbox rules over app/plugin.py
├── app/                 the application: what every front end calls
│   ├── interfaces.py    the App and Runtime protocols
│   ├── facade.py        LocalApp: the verbs over one home
│   ├── runtime.py       LocalRuntime: builds and joins the core; the provider -> channel factory
│   ├── ops.py           the CLI's operator verbs (status, publish, reconcile, ...)
│   ├── tools.py         the MCP tools (create_post, upload_media, delete_post, ...)
│   ├── plugin.py        the Orbit plugin's tools (status, validate, history, engagements, metrics, publish)
│   ├── approvals.py     the human verbs behind `pulsar approve | approvals | revoke`
│   ├── health.py, login.py, settings.py, writelog.py, importer.py
│   └── core/            the domain; only app uses it
│       ├── engagement/  reader: budgeted, recorded reads      ─▶ publishing, ledger, channels
│       ├── publishing/  plan, publisher, policy, media        ─▶ ledger, account, channels
│       ├── account/     aliases, registry, store, clients    ─▶ channels
│       ├── ledger/      the ledger: writes, reads, approvals and their usage
│       └── channels/    contract (Channel, AuthFlow, ...), credentials, loopback, x/, bluesky/
└── internal/            leaf utilities anyone may use; they use nothing above them
    ├── errors/          codes, exceptions
    ├── fs/              files, paths, jsonx
    └── guard/           the secret scanner
```

The rules, which `tests/test_layers.py` checks on every `make check`:

- **Front ends** (`cli`, `mcp`, `orbit`) import the modules of `app` and `internal`, never
  `app.core`: each front end's verbs live in `app` (`ops.py`, `tools.py`, `plugin.py`) and
  the front end keeps only its protocol (arguments, schemas, envelopes). The CLI starts the
  other two (`serve`, `orbit-tool`).
- **`app`**'s own modules consume `core` and join it into the verbs. Only `runtime.py` and
  `facade.py` name the implementations (`SqliteLedger`, `FernetFileStore`, `XChannel`,
  `XClient`, `BlueskyChannel`, `BlueskyClient`, `BlueskyLogin`); everything else types against
  the protocols (`App`, `Runtime`, `Ledger`, `CredentialStore`, `Channel`, `AuthFlow`, `XApi`).
- **A provider** is one package under `app/core/channels/` that stands on the contract alone.
  Outside it, only `runtime.py` (the provider -> channel factory) names it; X's login, health
  check and single-post tools predate the rule and are the listed exceptions.
- **`core`** never imports `app`. What it needs from the app, it declares as a protocol and is
  handed (the settings it reads, for example). Inside it, `engagement` stands on `publishing`
  (its policy and `Bound`), `ledger` and `channels`; `publishing` on `ledger`, `account` and
  `channels`; `account` on `channels`; `channels` and `ledger` on nothing else
  in `core`.
- **`internal`** imports nothing of pulsar outside `internal`.

A package with subpackages (`app`, `app/core`, `app/core/channels`, `cli`, `internal`) keeps its
`__init__.py` to a docstring, so importing one module beneath it loads only that module's
imports. A package without subpackages may re-export its modules' public names there.

`main.py` builds a `LocalApp` (the verbs, bound to one home and one transport) and hands it to
the CLI, which types it as `App`. Tests hand in a `LocalApp` on the fake transport the same way.

## Ambient state

Nothing below `main` reads the environment, the working directory or the home itself.
The entry point (`pulsar.main`) reads them once and builds the `App` with them; everything
below receives them from there:

- **Home.** `PULSAR_HOME`, else `~/.config/pulsar`; under Orbit, `$ORBIT_PLUGIN_STATE/home`
  (`app.runtime.default_paths`). The Orbit backend refuses a `PULSAR_HOME` that names another home
  (`orbit.backend.check_plugin_home`). Orbit runs the backend as `pulsar orbit-tool`, so it gets
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
