# pulsar

**pulsar** lets agents publish to social accounts (X today) as accounts a human bound on the
host, without ever holding a credential. Every post goes through one publisher: offline
checks, a secret scanner, a budget and daily-cap policy, and a ledger written before anything
is sent, so a retry never pays for a second post.

It runs three ways over one ledger: an MCP server, the `pulsar` CLI, and an
[Orbit](https://github.com/constellation-works/orbit) plugin. Reading timelines is not its job.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
```

## Authorize an account (human, once)

Create an X app with OAuth 2.0, type *Native app*, callback `http://127.0.0.1:8976/callback`.
Then, on the posting host (over SSH, forward the callback with `ssh -L 8976:127.0.0.1:8976`):

```sh
uv run pulsar auth login --account x:<handle> --client-id <CLIENT_ID>
uv run pulsar auth status            # add --live to prove the refresh works
```

pulsar checks with X that the token belongs to `@<handle>` before storing it, encrypted,
in the pulsar home (`PULSAR_HOME`, default `~/.config/pulsar`).

## Configure

Optional `config.toml` in the home: default account, expected handles, prices, budgets,
daily cap, quiet hours, media roots. Defaults: $1/day, $10/month, 5 posts per account per
day. See [the config reference](docs/design/publishing/references/config.md).

## Use

```sh
uv run pulsar validate plan.yaml          # what would be posted, its cost and digest; offline
uv run pulsar publish plan.yaml --confirm # post it (idempotent: re-running replays the receipt)
uv run pulsar status                      # budget and cap use, unresolved writes
uv run pulsar reconcile                   # settle posts whose outcome is unknown
uv run pulsar serve                       # MCP server (stdio)
uv run pulsar migrate --confirm           # after an upgrade: bring the home up to date
```

Output is a table on a terminal and tab-separated lines when piped; add `--json` (or set
`PULSAR_FORMAT=json`) for one JSON document. Errors go to stderr (exit 1, or 2 for a usage
error). `pulsar` alone lists the commands.

A plan:

```yaml
account: x:<handle>
posts:
  - text: "Orbit v0.26 is out"
    media: [{path: releases/v0.26/banner.png, alt: "The v0.26 banner"}]
  - text: "Notes: https://example.com/notes"
```

As an Orbit plugin (read-only tools today: `pulsar.status`, `pulsar.validate`,
`pulsar.history`), install from a commit export:

```sh
mkdir /tmp/pulsar-plugin && git archive agent-main | tar -x -C /tmp/pulsar-plugin
orbit plugin add /tmp/pulsar-plugin --enable --grant 'fs={{workspace}},{{plugin_state}}' --grant network
orbit pulsar status
```

The plugin's home is `~/.orbit/state/plugins/pulsar/home`; point the CLI at it with
`PULSAR_HOME`.

## Documentation

Design docs live in [docs/design](docs/design/) ([conventions](docs/design/CONVENTIONS.md),
[architecture](docs/design/ARCHITECTURE.md)):

- [Accounts](docs/design/accounts/1_overview.md): login, token storage, refresh.
- [Publishing](docs/design/publishing/1_overview.md): plans, the ledger, idempotency, policy,
  reconcile.
- [Channels](docs/design/channels/1_overview.md): the provider adapter and X.
- [Surfaces](docs/design/surfaces/1_overview.md): MCP tools, CLI, Orbit plugin,
  [error codes](docs/design/surfaces/references/error-codes.md).

## Development

```sh
make check    # uv lock --check, ruff, basedpyright strict, pytest (offline)
```

The tree under `src/pulsar` is the architecture ([ARCHITECTURE.md](docs/design/ARCHITECTURE.md)):
the front ends (`cli/`, `mcp/`, `orbit/`) call `app/`, whose modules consume the domain in
`app/core/`; `internal/` holds the leaf utilities everything uses.
