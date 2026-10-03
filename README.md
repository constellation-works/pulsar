# pulsar

**pulsar** lets agents publish to social accounts (X and Bluesky) as accounts a human bound on the
host, without ever holding a credential. Every post goes through one publisher: offline
checks, a secret scanner, a budget and daily-cap policy, and a ledger written before anything
is sent, so a retry never pays for a second post.

It runs three ways over one ledger: an MCP server, the `pulsar` CLI, and an
[Orbit](https://github.com/constellation-works/orbit) plugin. It reads only what engagement
needs (the account's mentions and its own posts' metrics), on the same budget, and never
stores what it reads.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/constellation-works/pulsar@v0.1.0
```

This puts `pulsar` on your PATH. From a checkout instead, `uv sync` and prefix each command
with `uv run`. Before upgrading, read the [changelog](CHANGELOG.md), then run
`pulsar migrate --confirm`.

## Authorize an account (human, once)

Create an X app with OAuth 2.0, type *Native app*, callback `http://127.0.0.1:8976/callback`.
Then, on the posting host (over SSH, forward the callback with `ssh -L 8976:127.0.0.1:8976`):

```sh
pulsar auth login --account x:<handle> --client-id <CLIENT_ID>
pulsar auth status            # add --live to prove the refresh works
```

pulsar checks with X that the token belongs to `@<handle>` before storing it, encrypted,
in the pulsar home (`PULSAR_HOME`, default `~/.config/pulsar`).

A Bluesky account needs no app registration; the same callback is used:

```sh
pulsar auth login --account bsky:<handle>
```

pulsar resolves the handle to its PDS, signs in through atproto OAuth (PAR, PKCE, DPoP), and
stores the tokens with their DPoP key only once the token's DID resolves back to the handle.
It uses the loopback development client unless `[oauth.bsky] client_id` in `config.toml`
names a hosted client-metadata document; publishing that document is a human step
([Accounts — Design §2](docs/design/accounts/2_design.md#2-login)).

## Configure

Optional `config.toml` in the home: default account, expected handles, prices, budgets,
daily cap, quiet hours, media roots. Defaults: $1/day, $10/month, 5 posts per account per
day. See [the config reference](docs/design/publishing/references/config.md).

## Use

```sh
pulsar validate plan.yaml          # what would be posted, its cost and digest; offline
pulsar publish plan.yaml --confirm # post it (idempotent: re-running replays the receipt)
pulsar status                      # budget and cap use, unresolved writes
pulsar reconcile                   # settle posts whose outcome is unknown
pulsar serve                       # MCP server (stdio)
pulsar migrate --confirm           # after an upgrade: bring the home up to date
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

`accounts: [x:<handle>, bsky:<handle>]` publishes one plan to both, and
`variants: {bsky: {posts: [...]}}` gives one provider its own posts.

As an Orbit plugin (`pulsar.status`, `validate`, `history`; and, from tasks that require
them, `engagements`, `metrics` and `publish`, which publishes only what a human approved with
`pulsar approve`), install from an export of the release tag, in a clone:

```sh
mkdir /tmp/pulsar-plugin-v0.1.0 && git archive v0.1.0 | tar -x -C /tmp/pulsar-plugin-v0.1.0
orbit plugin add /tmp/pulsar-plugin-v0.1.0 --enable --grant 'fs={{workspace}},{{plugin_state}}' --grant network
orbit pulsar status
```

Orbit installs only `.orbit-plugin/` from that export. In a checkout, run
`make plugin` after changing the Python package and commit the generated copy
before tagging a release.

The plugin's home is `~/.orbit/state/plugins/pulsar/home`; point the CLI at it with
`PULSAR_HOME`. Enabling it in a workspace seeds five auto-tasks, switched off: `auth-health`
(daily offline account check), `engager` (daily mention summary and reply drafts),
`post-proposer` (weekly post drafts from metrics), `weekly-report` and `x-updates` (drafts
for new constellation-works releases, repos and PRs, each under a key that posts once).

## Documentation

Design docs live in [docs/design](docs/design/) ([conventions](docs/design/CONVENTIONS.md),
[architecture](docs/design/ARCHITECTURE.md)):

- [Accounts](docs/design/accounts/1_overview.md): login, token storage, refresh.
- [Publishing](docs/design/publishing/1_overview.md): plans, the ledger, idempotency, policy,
  reconcile.
- [Channels](docs/design/channels/1_overview.md): the provider adapter, X and Bluesky.
- [Surfaces](docs/design/surfaces/1_overview.md): MCP tools, CLI, Orbit plugin,
  [error codes](docs/design/surfaces/references/error-codes.md).

## Development

```sh
make check    # uv lock --check, ruff, basedpyright strict, pytest (offline)
make plugin   # refresh the committed runtime copy in .orbit-plugin/
```

The tree under `src/pulsar` is the architecture ([ARCHITECTURE.md](docs/design/ARCHITECTURE.md)):
the front ends (`cli/`, `mcp/`, `orbit/`) call `app/`, whose modules consume the domain in
`app/core/`; `internal/` holds the leaf utilities everything uses.
