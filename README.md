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
   uv run pulsar auth status            # cached account, no X call once cached
   uv run pulsar auth status --live     # prove it: forced refresh + GET /2/users/me
   uv run pulsar auth status --offline  # stored state only, never calls X
   ```

   The default reads `whoami.json`, so it names the account even when the
   refresh token is already dead; it says so (`verified: false` and a `note`,
   `token_state: expired` when the access token has lapsed). `--live` is the
   proof: it rotates the token pair through the refresh lock, fetches the
   account from X, and rewrites the cache. It costs one `/users/me` read.
   The cache is tagged with the login it describes (a `binding_id` minted by
   `auth login` and carried across refreshes), so after a re-login it is
   ignored until `/users/me` has been asked again, even if a lookup started
   before the re-login finishes after it.

Refresh happens automatically. When a refresh fails (token revoked, app reset),
tools return `auth_expired` and a human re-runs `auth login`.

Several pulsar processes may share one home (a stdio server per client, plus
`auth status`). X rotates the refresh token on every use, so refreshes are
serialised across processes with an exclusive `flock` on `refresh.lock`: the
first process refreshes, the others wait (up to 45 s, then `api_error`) and
reuse the bundle it saved. If X still rejects a refresh token because a
process that ignores the lock (an older pulsar mid-upgrade) rotated it first,
pulsar re-reads the store and uses the newer bundle instead of reporting
`auth_expired`. `auth login` and `auth logout` take the same lock, so a
refresh already in flight can never write the previous account's rotated
tokens over a new login, or back after a logout.

## Configuration

Optional `config.toml` in the pulsar home. Every key has a default, and an
unknown key or bad value fails with `invalid_config` rather than silently
falling back:

```toml
[prices]                 # USD per post, used for estimated_cost_usd
plain_post_usd = 0.015   # X changes its price list; verify on the developer portal
url_post_usd = 0.20

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none (path uploads off)
```

`media.roots` are the only directories `upload_media` will read a `path` from
(see [Media confinement](#media-confinement)). With none set, a `path` upload
is refused with `invalid_config` and only `base64` works: the server's cwd is
not a safe default, since some MCP hosts start servers in `/` or `$HOME`.
Roots must be absolute (`~` is expanded), and `/`, the home directory and its
ancestors are refused; name the directory the media lives in. `pulsar serve`
prints the effective roots to stderr at startup.

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
| `create_post` | publishes | `POST /2/tweets` | `text`, optional `reply_to_post_id`, `quote_post_id`, `media_ids`, `idempotency_key`, `dry_run` (legacy) |
| `upload_media` | publishes | `POST /2/media/upload/initialize` → `/{id}/append` → `/{id}/finalize`; `GET /2/media/upload` for video status | png/jpeg/gif/webp images ≤5 MiB or MP4 video (`video/mp4`) ≤100 MiB; `path` (a regular file inside `media.roots`) or `base64`, optional `mime` (must match the sniffed content) → `{media_id}` after video processing succeeds |
| `delete_post` | destructive | `DELETE /2/tweets/:id` | `post_id` (numeric X id), optional `idempotency_key` (default `delete:<post_id>`) → `{ok: true, post_id, deleted}` |

`validate_post` returns `{ok: true, text, weighted_length, has_url,
estimated_cost_usd}` without touching the network. `create_post` returns
`{ok: true, post_id, url, text}`; `dry_run: true` returns what `validate_post`
does plus `dry_run: true`, and is kept for callers that predate `validate_post`;
a dry run is not a write and records nothing.
Every writing tool takes an optional `caller` (agent id) for the ledger;
`PULSAR_CALLER` in the server's environment is the fallback. `caller` is
**advisory**: a self-asserted audit label, not an identity pulsar verifies.

#### Idempotency

Every post costs money and X has no idempotency key of its own, so pulsar
keeps one. `create_post` takes an optional `idempotency_key` (1–200
characters, no whitespace or control characters); without one, the key is
derived from the request (text, `reply_to_post_id`, `quote_post_id`,
`media_ids`) and the bound account's user id. The key is recorded in the
[ledger](#the-ledger) *before* the request is sent. Calling again with the same
key:

- **already published** → the stored receipt `{ok: true, post_id, url, text,
  replayed: true}`; nothing is sent to X.
- **different request** (other text, reply target, media, tool or account) →
  `idempotency_conflict`; nothing is sent. Use a new key for a new write.
- **earlier attempt failed definitively** (the request provably never reached
  X, or X rejected it) → retried.
- **earlier attempt's outcome is unknown, or still in flight** →
  `outcome_unknown` again; nothing is sent.

Because of the derived key, posting the exact same text again from the same
account replays the first receipt. To post identical text deliberately (say,
after deleting the original), pass a fresh `idempotency_key`.

`delete_post` uses the same mechanism with the default key `delete:<post_id>`:
repeating a delete that succeeded returns `{ok: true, post_id, deleted,
replayed: true}` without calling X. `upload_media` records every upload in the
ledger but does not deduplicate: an orphaned media id is harmless and expires.

#### `outcome_unknown`

Returned when the post request may have reached X but pulsar cannot tell
whether X created the post: the connection dropped or timed out after the
request was sent (`ReadTimeout`, `WriteTimeout`, `ReadError`,
`RemoteProtocolError`, …), X answered 5xx, or X answered 2xx without a
readable post id. The ledger row is left `unknown` and `detail` carries the
`cause` and the `idempotency_key`. **Do not retry blindly** — a retry that
succeeds is a second, paid post. Check the account's timeline; if the post is
not there and you still want it, call again with a *new* `idempotency_key`. A
later phase adds reconcile, which settles unknown rows against X. Failures
that prove nothing was sent (`ConnectError`, `ConnectTimeout`, `PoolTimeout`)
stay `api_error` with `retryable: true`.

The *Annotation* column is what the server advertises through MCP tool
annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`). See
[The caller boundary](#the-caller-boundary) for why.

Failures never raise into the client (an unexpected exception becomes a
non-retryable `api_error` naming its type); they come back as
`{ok: false, code, message, retryable, detail?}` so the agent can branch on
`code`. `retryable` is true only when repeating the identical call later can
succeed (`rate_limited`, `api_error`):

| code | meaning | what to do |
|---|---|---|
| `auth_expired` | no token, or refresh failed (revoked / app reset) | stop; a human runs `pulsar auth login` |
| `insecure_storage` | the pulsar home is wider than 0700, or `key` / `tokens.enc` wider than 0600 or not owned by the server's user | stop; a human runs the `chmod` in `message` (also `detail.fix`) — not a re-login |
| `invalid_config` | `config.toml` has an unknown key or a bad value (including a media root that is `/`, `~` or above it), the ledger is from a newer pulsar, or a `path` upload with no `media.roots` configured | fix the file named in `message`; for uploads, pass `base64` or have the operator set roots |
| `invalid_text` | empty, over 280 weighted chars, control chars, reply+quote together | rewrite |
| `invalid_argument` | malformed `idempotency_key`, or a `post_id` / `reply_to_post_id` / `quote_post_id` / `media_ids` entry that is not a 1–19 digit X id | fix the argument |
| `secret_detected` | text (or media, or `idempotency_key`) matches a credential pattern | rewrite; never retry verbatim |
| `invalid_media` | bad path/base64, path outside `media.roots` or not a regular file, content that is not png/jpeg/gif/webp/mp4 or does not match the declared/extension MIME (`detail: {declared, sniffed}`), oversized media, or failed/timed-out video processing | fix the input or inspect X's processing detail |
| `duplicate` / `forbidden` / `rate_limited` / `not_found` | X's reason, passed through in `detail` | duplicate: change text; rate_limited: wait |
| `idempotency_conflict` | the key was already used for a different request or account | use a new key |
| `outcome_unknown` | the write may have reached X; see [above](#outcome_unknown) | do **not** retry; check the timeline |
| `api_error` | anything else from X, a network failure before the request was sent, or a token refresh that failed without X rejecting the refresh token (unreadable response, save failed) | retry later, report |

The 100 MiB video cap is a local connector limit; X also checks the account's
video size and duration entitlement when media is uploaded and attached to a
post. Upload MP4 by file path or as a base64 payload; the type is sniffed from
the content either way (see below). Video uses 4 MiB chunks and waits up to five
minutes for X's processing state to become `succeeded` before returning a
`media_id`. If X reports `failed` or processing times out, `upload_media`
returns `invalid_media` with the last processing detail and no `media_id`.
Each upload's ledger row (and `writes.jsonl` line) records MIME, byte count,
processing state, and — on success — the `media_id`; never the media bytes or
credentials.

#### Media confinement

`upload_media` publishes whatever bytes it reads, so a file path is an
exfiltration route (`path: "~/.ssh/id_rsa", mime: "image/png"`). The secret
scanner is not the defence — most secrets match none of its patterns. Instead:

- **Roots.** `path` is `~`-expanded and fully resolved (symlinks followed;
  a relative path resolves against the server's cwd). The result must be
  inside one of `media.roots`; with none configured, path uploads are off. A symlink that points
  outside the roots is refused; one that stays inside is fine. The pulsar home
  is refused even when a root contains it.
- **Regular files only.** Directories, FIFOs and devices are refused before
  they are opened. The file is opened by walking down from the root without
  following symlinks, and the opened file must be the one that was checked, so
  swapping the path after the check does not redirect the read.
- **Size before read.** The size limit is checked from the open file's
  metadata before any byte is read, and the read itself stops one byte past
  the limit.
- **Content decides the type.** The leading bytes must be PNG, JPEG, GIF, WebP
  or MP4 (ISO BMFF `ftyp`; QuickTime and HEIF/AVIF brands are refused). A
  `mime` argument — or, without one, the file extension — that disagrees with
  the content is refused, as is content with no recognised signature. `mime`
  is optional; the returned `mime` is always the sniffed one. The same rules
  apply to `base64` payloads.
- Errors name paths and types, never file contents.

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

The `caller` argument and the ledger are audit, not enforcement: they record
who *claimed* to make a write. `caller` is advisory and self-asserted; nothing
authenticates it.

### Safety

- No tool accepts a token, key, or secret argument. Credentials never cross the
  MCP boundary in either direction.
- Every live write goes through the ledger (below) and ends as a line in
  `writes.jsonl`. Validation and dry runs are not writes and record nothing.
- Text that looks like a secret (`sk-…`, `ghp_…`, `github_pat_…`, `xoxb-…`,
  AWS keys, PEM blocks, …) is rejected with `secret_detected` before any
  network call — including on `dry_run`. Media bytes are scanned for the same
  patterns before upload, after the confinement and type checks in
  [Media confinement](#media-confinement).
- `create_post` is meant to be called only on explicit user intent in the
  calling chat, or from a standing routine the owner enabled. The connector
  cannot verify intent; that rule lives with the caller (and is repeated in the
  tool description and server instructions).
- Encrypted-at-rest means Fernet with a key file beside the bundle (both 0600,
  directory 0700). Saves are atomic (temp file, `fsync`, rename), so a crash
  mid-refresh never destroys the only refresh token, and concurrent first
  saves agree on one key. Every file pulsar creates in its home — `key`,
  `tokens.enc`, `client.json`, `whoami.json`, `writes.jsonl`, `refresh.lock` —
  is 0600 regardless of umask.
- What that protects against: the bundle leaking in plaintext through
  backups, `cat`/`grep` over the home, a stray commit, or another local user.
  pulsar refuses to load or save credentials when the home is wider than
  0700, or `key`/`tokens.enc` wider than 0600 or owned by another uid
  (`insecure_storage`, with the exact `chmod` fix), rather than use a token
  others could have copied. Nothing writes into a home that already exists
  wider than 0700 either, and pulsar never narrows one silently: how long it
  was open is the operator's call to judge. A corrupt `key` file is also
  `insecure_storage`.
- What it does not protect against: any process running as the same uid can
  read the key and the ciphertext and decrypt them. On the Mac that includes
  an Orbit-sandboxed worker that can read `~/.config`. The long-term fix is
  host-held secrets outside the user's files (ORB-13008, ORB-13009); the
  store sits behind a `CredentialStore` interface so that can drop in. Until
  then the boundary is: the *agent* never holds secrets; the connector
  process does.

### The ledger

`~/.config/pulsar/ledger.sqlite3` (under `PULSAR_HOME`) is the source of truth
for writes: SQLite in WAL mode, created 0600 in the 0700 home, schema version
in `PRAGMA user_version`. Several pulsar processes may share one home; claims
take the write lock (`BEGIN IMMEDIATE`, busy timeout) so two of them cannot
send the same key.

One row per logical write (`writes` table): `idempotency_key` (unique), `tool`,
the account's `account_user_id`/`account_handle`, the advisory `caller`,
`request_digest` (SHA-256 of the canonical request), `text_sha256`, `state`,
`post_id`/`media_id`/`url`, `error_code`/`error_message`/`retryable`,
`meta_json` (mime, bytes, processing_state, deleted), `attempts`,
`created_at`/`updated_at`. States:

```
submitting ──> published   X confirmed; the key replays this receipt
           ├─> failed      nothing reached X, or X rejected it; a retry re-sends
           └─> unknown     may have reached X; never re-sent automatically
```

The row is committed as `submitting` *before* the request leaves, so a crash
or kill mid-request leaves evidence, and that key answers `outcome_unknown`
until it is reconciled. Inspect it with `sqlite3 ledger.sqlite3 'select * from
writes'`.

`writes.jsonl` beside it is an **export**, append-only: one line per terminal
transition (`published`, `failed`, `unknown`) with `ts`, `tool`, `caller`,
`dry_run` (always false now), `post_id`, `text_sha256`, `state`,
`idempotency_key`, `account_user_id`, `error_code` on failure, and the upload
facts — never post text, media bytes, or credentials.

## Development

```sh
make check     # ruff lint + format check + pytest (no network; fake X transport)
```

`tests/conftest.py` holds the scripted X API; tests drive the server through a
real MCP client session over the in-memory transport.
