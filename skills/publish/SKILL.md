---
name: pulsar-publish
description: Check pulsar's publishing accounts, budget and recent posts, and validate a post or thread plan offline before anyone publishes it, through the pulsar.* Orbit tools. Also walks a human through setting pulsar up (install, the X app, binding an account) when it is missing or unhealthy.
---

# pulsar: publishing through one ledger

pulsar publishes to social accounts (X today) for the constellation. Every write
goes through one ledger with a budget, a daily cap per account, a secret scanner
and idempotent retries. The plugin's tools only read state or validate; none of
them posts, uploads or deletes.

| Tool | Use it to |
|---|---|
| `pulsar.status` | see each account's token health, the day and month budget, posts today against the cap, unresolved writes and the last publication; `healthy` and `attention` summarise what needs a human |
| `pulsar.validate` | check a plan (inline `plan`, or a workspace-relative YAML `source`) and get, per account, the exact posts, weighted lengths, media facts, estimated cost and the plan `digest` |
| `pulsar.history` | list the newest ledger rows (`limit` 1–100, optional `account`) with state, URL and cost |

From a shell: `orbit pulsar status`, `orbit pulsar validate <plan.yaml>`,
`orbit pulsar history`.

## When a write is justified

Only when a person explicitly asked for this post, reply, quote or delete in this
conversation, or when a standing routine Daniel enabled fires. If the intent is
implied rather than stated, ask. Reading timelines, search and metrics are not
pulsar's job.

## Draft and validate

1. Call `pulsar.status`. If `healthy` is false, read `attention` and stop: an
   account that needs a login, or writes with an unknown outcome, are for a human.
2. Write the plan. One post is `text:` (plus optional `media:`); a thread is
   `posts:`, a list of those. Every media item needs `alt`, and its `path` must lie
   inside the workspace. `reply_to` or `quote`, never both. `account` (for example
   `x:<handle>`) or `accounts:`; omitted means the default account.
   ```yaml
   account: x:<handle>
   posts:
     - text: "Orbit v0.26 is out"
       media: [{path: releases/v0.26/banner.png, alt: "The v0.26 banner"}]
     - text: "Notes: https://example.com/notes"
   ```
3. Call `pulsar.validate` with it. Nothing is sent or recorded. `valid: false`
   carries `error.code`, `error.message` and `error.detail`; fix and repeat.
   Keep posts within 280 weighted characters (a URL counts 23, CJK and emoji 2).
   A post with a URL costs about $0.20, a plain one about $0.015.
4. Report the posts, the estimated cost and the `digest` to whoever will publish.
   Publishing is not a plugin tool yet: an operator runs
   `pulsar publish PLAN.yaml --confirm` on the posting host.

## Error codes

| code | do |
|---|---|
| `auth_expired`, `account_mismatch` | stop; a human runs `pulsar auth login --account <alias>` |
| `unknown_account` | use an alias from `detail.known`, or ask a human to bind it |
| `invalid_text`, `invalid_plan`, `invalid_media`, `unsupported` | fix the plan (`detail` says where) |
| `secret_detected` | rewrite the text; never retry it verbatim |
| `budget_exceeded`, `daily_cap` | wait until `detail.retry_after`, or split the plan |
| `insecure_storage`, `invalid_config` | stop; a human fixes the home named in `message` |
| `invalid_argument` | fix the tool input |

## Setup

When pulsar is not installed, no account is bound, or `status` names a login,
storage or config problem a human must fix, follow
[references/setup.md](references/setup.md): it walks a human through
installing pulsar, creating the X app, binding an account and connecting the
plugin and MCP server, one checked step at a time.

## Credentials

No pulsar tool accepts or returns a token, key or secret, and none ever will.
Never paste one into a plan or an argument. The tokens live encrypted in the
plugin's state (`~/.orbit/state/plugins/pulsar/home`). A human authorizes an
account on the posting host, forwarding the OAuth callback when working over SSH:

```bash
ssh -L 8976:127.0.0.1:8976 <posting-host>
cd <pulsar checkout> && PULSAR_HOME=~/.orbit/state/plugins/pulsar/home \
  uv run pulsar auth login --account x:<handle> --no-browser
```

pulsar checks with X that the token belongs to `<handle>` before storing it.
