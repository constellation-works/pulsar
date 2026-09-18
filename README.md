# pulsar

**pulsar** is an X (Twitter) write connector: a small MCP server that lets an
agent (an Orbit routine, a Claude or Codex session, a bot) post as **whichever
X account a human authorized on the host** — without a browser login or a
Bearer token pasted into chat. One connector instance is bound to one account;
run a second instance with its own `PULSAR_HOME` to post as another.

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

   A browser opens, you approve as the account that should post, and pulsar
   stores the token bundle encrypted under `~/.config/pulsar/` (override with
   `PULSAR_HOME`).
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

| Tool | Annotation | X endpoint | Notes |
|---|---|---|---|
| `whoami` | read-only | `GET /2/users/me` | cached; `{user_id, username}` of the bound account |
| `validate_post` | read-only | — | `text`, optional `reply_to_post_id`, `quote_post_id`; no network, no log |
| `create_post` | publishes | `POST /2/tweets` | `text`, optional `reply_to_post_id`, `quote_post_id`, `media_ids`, `dry_run` (legacy) |
| `upload_media` | publishes | `POST /2/media/upload/initialize` → `/{id}/append` → `/{id}/finalize` | images only in v1; `path` or `base64` + `mime` → `{media_id}` |
| `delete_post` | destructive | `DELETE /2/tweets/:id` | `post_id` → `{ok: true}` |

`validate_post` returns `{ok: true, text, weighted_length, has_url,
estimated_cost_usd}` without touching the network. `create_post` returns
`{ok: true, post_id, url, text}`; `dry_run: true` returns what `validate_post`
does plus `dry_run: true`, and is kept for callers that predate `validate_post`.
Every writing tool takes an optional `caller` (agent id) for the write log;
`PULSAR_CALLER` in the server's environment is the fallback.

The *Annotation* column is what the server advertises through MCP tool
annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). See
[The caller boundary](#the-caller-boundary) for why.

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

### The caller boundary

pulsar does not decide whether a post *should* go out. It cannot: it sees a
tool call, not the conversation that led to it. Intent enforcement is the
caller's job — the agent harness, the MCP client's permission layer, or a
routine's own guard.

What pulsar does do is make that boundary legible to a policy layer that gates
by tool name and annotations, which is how most harness permission systems
work:

- `whoami` and `validate_post` carry `readOnlyHint: true`. A harness can
  auto-allow them; nothing leaves the host.
- `create_post` and `upload_media` carry `readOnlyHint: false,
  destructiveHint: false`. They publish; prompt on them.
- `delete_post` carries `destructiveHint: true`. Prompt harder.

`create_post(dry_run=true)` predates `validate_post` and still works, but it is
the same tool name as a live post — a policy engine that gates by name cannot
tell them apart without parsing arguments. New callers should validate with
`validate_post` and reserve `create_post` for the moment intent is established.

The `caller` argument and the write log are audit, not enforcement: they record
who claimed to make a write, after the fact.

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
  calling chat, or from a standing routine the owner enabled. The connector
  cannot verify intent; that rule lives with the caller (and is repeated in the
  tool description and server instructions).
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
