# pulsar

**pulsar** is the constellation's X (Twitter) write connector: a small MCP server
that lets an agent (Grok Bot, an Orbit routine, a Claude session) post as
**@constworks** without a browser login or a Bearer token pasted into chat.

Auth belongs to the connector process, never to the agent. pulsar holds an
OAuth 2.0 user token (PKCE, `tweet.read tweet.write users.read offline.access`),
keeps the refresh token encrypted on the host, and exposes only intent-level
tools: `whoami`, `create_post`, `upload_media`, `delete_post`.

Reads (timeline, search) are out of scope — the existing X plugin covers them.

## Install

Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) are required.

```sh
uv sync
```

## One-time authorization (human, in a browser)

1. In the X developer portal, create an app with **OAuth 2.0** enabled, type
   *Native app* (public client, PKCE), callback `http://127.0.0.1:8976/callback`.
2. Run the login flow on the host that will run the connector:

   ```sh
   uv run pulsar auth login --client-id <CLIENT_ID>
   ```

   A browser opens, you approve as @constworks, and pulsar stores the token
   bundle encrypted under `~/.config/pulsar/` (override with `PULSAR_HOME`).
3. Check the binding:

   ```sh
   uv run pulsar auth status
   ```

Refresh happens automatically. When a refresh fails (token revoked, app reset),
tools return `auth_expired` and a human re-runs `auth login`.

## Run as an MCP server

```sh
uv run pulsar serve            # stdio transport
```

Register with a client, e.g. Claude Code:

```sh
claude mcp add pulsar -- uv --directory /path/to/pulsar run pulsar serve
```

### Tools

| Tool | X endpoint | Notes |
|---|---|---|
| `whoami` | `GET /2/users/me` | cached; `{user_id, username}` of the bound account |
| `create_post` | `POST /2/tweets` | `text`, optional `reply_to_post_id`, `quote_post_id`, `media_ids`, `dry_run` |
| `upload_media` | `POST /2/media/upload` | images only in v1; `path` or `base64` + `mime` → `{media_id}` |
| `delete_post` | `DELETE /2/tweets/:id` | `post_id` → `{ok: true}` |

`create_post` returns `{ok, post_id, url, text}`; with `dry_run: true` it
validates only and returns `estimated_cost_usd`.

Errors are structured: `auth_expired`, `invalid_text`, `secret_detected`,
`duplicate`, `forbidden`, `rate_limited`, `api_error`.

### Safety

- No tool accepts a token, key, or secret argument. Credentials never cross the
  MCP boundary in either direction.
- Every write is appended to `~/.config/pulsar/writes.jsonl`: timestamp, tool,
  post_id, SHA-256 of the text, and the caller agent id (`PULSAR_CALLER` env or
  the `caller` argument).
- Text that looks like a secret (`sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`,
  AWS keys, PEM blocks, …) is rejected with `secret_detected` before any
  network call — including on `dry_run`.
- `create_post` is meant to be called only on explicit user intent in the
  calling chat, or from a standing routine Daniel enabled. The connector cannot
  verify intent; that rule lives with the caller.

## Development

```sh
make check     # lint + tests
```
