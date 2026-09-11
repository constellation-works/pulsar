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
| `upload_media` | `POST /2/media/upload/initialize` → `/{id}/append` → `/{id}/finalize` | images only in v1; `path` or `base64` + `mime` → `{media_id}` |
| `delete_post` | `DELETE /2/tweets/:id` | `post_id` → `{ok: true}` |

`create_post` returns `{ok: true, post_id, url, text}`; with `dry_run: true` it
validates only (no network call) and returns `weighted_length`, `has_url` and
`estimated_cost_usd`. Every tool takes an optional `caller` (agent id) for the
write log; `PULSAR_CALLER` in the server's environment is the fallback.

Failures never raise into the client; they come back as
`{ok: false, code, message, detail?}` so the agent can branch on `code`:

| code | meaning | what to do |
|---|---|---|
| `auth_expired` | no token, or refresh failed (revoked / app reset) | stop; a human runs `pulsar auth login` |
| `invalid_text` | empty, over 280 weighted chars, control chars, reply+quote together | rewrite |
| `secret_detected` | text matches a credential pattern | rewrite; never retry verbatim |
| `invalid_media` | bad path/base64, non-image, >5MB | fix the input |
| `duplicate` / `forbidden` / `rate_limited` / `not_found` | X's reason, passed through in `detail` | duplicate: change text; rate_limited: wait |
| `api_error` | anything else from X or the network | retry later, report |

Off-box callers (Grok Bot) can use the streamable-HTTP transport instead of
stdio: `uv run pulsar serve --transport http --port 8977` binds loopback only;
exposing it (Caddy, tailnet) and putting auth in front is an operator step.

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
  verify intent; that rule lives with the caller (and is repeated in the tool
  description and server instructions).
- Encrypted-at-rest means Fernet with a key file beside the bundle (both 0600,
  directory 0700). It protects against backups, `cat`, and stray commits — not
  against a compromised host account. That is the intended boundary: the
  *agent* never holds secrets; the connector process does.

## Development

```sh
make check     # ruff lint + format check + pytest (no network; fake X transport)
```

`tests/conftest.py` holds the scripted X API; tests drive the server through a
real MCP client session over the in-memory transport.
