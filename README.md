# pulsar

**pulsar** is an X (Twitter) write connector: a small MCP server that lets an
agent (an Orbit routine, a Claude or Codex session, a bot) post as **the X
accounts a human bound on the host** — without a browser login or a Bearer
token pasted into chat. One pulsar home holds several accounts, each named by
an alias (`x:constworks`); a call picks one with `account`, or gets the
operator's default.

Auth belongs to the connector process, never to the agent. pulsar holds an
OAuth 2.0 user token (PKCE, `tweet.read tweet.write users.read offline.access`),
keeps the refresh token encrypted on the host, and exposes only intent-level
tools: `whoami`, `validate_post`, `validate_plan`, `create_post`,
`upload_media`, `delete_post`. Every post goes through one publisher: offline
checks, the secret scanner, the policy (budgets, daily cap, quiet hours) and
the ledger, before anything is sent. The operator side is the `pulsar` CLI
([Operator commands](#operator-commands)).

Reads (timeline, search) are out of scope — the existing X plugin covers them.

## Install

Python 3.12+ and [`uv`](https://docs.astral.sh/uv/) are required.

```sh
uv sync
```

## One-time authorization (human, in a browser)

1. In the X developer portal, create an app with **OAuth 2.0** enabled, type
   *Native app* (public client, PKCE), callback `http://127.0.0.1:8976/callback`.
2. Bind each account on the host that will run the connector, naming it
   first:

   ```sh
   uv run pulsar auth login --account x:constworks --client-id <CLIENT_ID>
   uv run pulsar auth login --account x:otherhandle     # client id is remembered
   ```

   A browser opens and you approve as that account. Before anything is
   stored, pulsar asks X (`GET /2/users/me`) whom the new token belongs to. If
   the handle is not the alias's (`constworks` for `x:constworks`), or not the
   configured `expected_handle`, the login is refused with `account_mismatch`
   naming both handles, nothing is written, and it exits 1 — log out of X in
   the browser and approve as the right account. This replaces the manual
   "make sure it is @constworks" step: on 2026-09-16 posts went to the wrong
   account because the wrong token had been stored. `--account` defaults to
   `default_account` from the config.

   Tokens are stored encrypted under `~/.config/pulsar/` (override with
   `PULSAR_HOME`): one bundle per account in `accounts/<provider>--<handle>/`,
   one `key` for all of them, the X app's client id in `client.json` (one per
   provider), and the account registry in `accounts.json` (alias, provider
   user id, handle, scopes, `status` = `active` | `reauth_required` |
   `revoked`, `bound_at`, `binding_id`, `verified_at`; no secrets).
3. Check the bindings:

   ```sh
   uv run pulsar auth status                        # every account, cached, no X call once cached
   uv run pulsar auth status --account x:constworks # just one
   uv run pulsar auth status --live                 # prove each: forced refresh + GET /2/users/me
   uv run pulsar auth status --offline              # stored state only, never calls X
   ```

   It prints one entry per account under `accounts`, each with `alias`,
   `status`, `expected_handle`, `mismatch` (the bound handle is not the
   expected one; `null` while unknown), `token_state`, `account`,
   `account_source`, `verified`, `reauth_required`, `healthy` and a `note`
   when unproven, and exits 0 only when every reported account is healthy.
   The default reads each account's identity from the registry, so it names
   the account even when the refresh token is already dead; it says so
   (`verified: false` and a `note`, `token_state: expired` when the access
   token has lapsed). `--live` is the proof: it rotates each account's token
   pair through that account's refresh lock, fetches the account from X, and
   rewrites the registry row. It costs one `/users/me` read per account. The
   row is tagged with the login it describes (a `binding_id` minted by
   `auth login` and carried across refreshes), so after a re-login it is
   ignored until `/users/me` has been asked again, even if a lookup started
   before the re-login finishes after it.
4. `uv run pulsar auth logout --account x:constworks` deletes that account's
   tokens (under its refresh lock); its registry row stays, `status: revoked`,
   for history.

Refresh happens automatically. When a refresh fails (token revoked, app reset),
tools return `auth_expired`, the account is marked `reauth_required` in the
registry, and a human re-runs `auth login --account …` (or `auth status --live`
once the refresh works again).

**Upgrading from the single-account layout.** A home from before accounts
existed has `tokens.enc` and `whoami.json` at its root. The first pulsar
command or tool call moves the bundle to `accounts/x--<handle>/` as
`default_account` if one is configured, else as `x:<username>` from a
`whoami.json` that describes the stored login. If neither names it, the bundle
stays put and every call says `legacy credentials need an alias: run
\`pulsar auth migrate --account x:<handle>\``. A cached identity that
contradicts the alias is `account_mismatch` and moves nothing, and migration
never overwrites an account that already has credentials. The move holds the
old root refresh lock, renames the bundle, then writes the registry row, then
removes `whoami.json`; re-running after a crash at any step finishes the job.

Several pulsar processes may share one home (a stdio server per client, plus
`auth status`). X rotates the refresh token on every use, so each account's
refreshes are serialised across processes with an exclusive `flock` on its
`accounts/<slug>/refresh.lock`: the first process refreshes, the others wait
(up to 45 s, then `api_error`) and reuse the bundle it saved. Accounts do not
wait for each other. If X still rejects a refresh token because a process that
ignores the lock (an older pulsar mid-upgrade) rotated it first, pulsar
re-reads the store and uses the newer bundle instead of reporting
`auth_expired`. `auth login` and `auth logout` take the same per-account lock
(and update the registry before releasing it), so a refresh already in flight
can never write the previous account's rotated tokens over a new login, or
back after a logout.

## Configuration

Optional `config.toml` in the pulsar home. Every key has a default, and an
unknown key or bad value fails with `invalid_config` rather than silently
falling back:

```toml
default_account = "x:constworks"     # the account a call without `account` acts as

[accounts."x:constworks"]
expected_handle = "constworks"       # bound handle must match, else account_mismatch

[prices.x]                           # USD per post, used for estimated_cost_usd and budgets
plain_post_usd = 0.015               # X changes its price list; verify on the developer portal
url_post_usd = 0.20                  # (flat plain_post_usd/url_post_usd under [prices] = X)

[policy]                             # checked before any network call
daily_budget_usd = 1.0               # all accounts; 0 stops all paid publishing
monthly_budget_usd = 10.0
max_posts_per_day = 5                # per account; every post of a thread counts
quiet_hours = "23:00-07:00"          # optional; default none
timezone = "UTC"                     # IANA zone for the day/month boundary and quiet hours

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none (path uploads off)
```

Account aliases are `provider:handle` and case-insensitive (`X:@ConstWorks`
is `x:constworks`); the handle may use `a-z 0-9 . _ -` (no leading dot, at
most 100 characters), anything else is `invalid_argument`. A provider with no
`[prices.<provider>]` table is priced at zero.

`default_account` and `expected_handle` are enforced. A call that names no
`account` acts as `default_account`, else as the only bound (not revoked)
account; with several bound and no default it is `invalid_argument` ("several
accounts are bound; name one"), and an alias that is not registered is
`unknown_account` (the message lists the known ones). Before every write, and
at login, the handle the credentials belong to must equal the alias's handle
and, if set, `expected_handle`; otherwise `account_mismatch` with `detail:
{alias, expected_handle, bound_handle}` and nothing is sent. The `[policy]`
keys are enforced on every post, from `create_post` and from `pulsar publish`
alike.

Policy (`pulsar.core.policy`) is checked before any network call, against
what the ledger has committed or reserved (in-flight, published or
unknown-outcome posts, and the unsent posts of a plan still being published;
failed and skipped ones are free). The first rule that fails is reported:

1. `quiet_hours` — now is inside the window. `[start, end)` on the wall clock
   of `policy.timezone`; it may wrap midnight (`23:00-07:00`).
2. `daily_cap` — the account's posts today plus the plan's posts exceed
   `max_posts_per_day`. Every post of a thread counts.
3. `budget_exceeded` — spend today (all accounts) plus the plan's estimated
   cost exceeds `daily_budget_usd`; then the same for the calendar month and
   `monthly_budget_usd` (`detail.window` is `day` or `month`).

Limits are inclusive: a plan that lands exactly on a cap or budget passes.
Money is compared in exact decimals (6 places). A budget of 0 stops every
paid plan and a cap of 0 every post; a plan that costs nothing is never
stopped by a budget, only by the cap. All three codes are `retryable` and
carry `detail.retry_after` (ISO 8601 UTC): the end of the quiet window, the
next local midnight, or the first instant of the next local month. A plan too
big to ever pass on its own — more posts than the cap, or costing more than a
budget — is refused with `retryable: false` and `retry_after: null`; split it
or raise the limit.

Days and months are local to `policy.timezone` (default `UTC`), so a day is
23 or 25 hours across a DST change. A quiet-hours end skipped by a
spring-forward gap ends the window at the jump; on a fall-back night a
repeated wall-clock hour inside the window is quiet both times.

`media.roots` are the only directories `upload_media` will read a `path` from
(see [Media confinement](#media-confinement)). With none set, a `path` upload
is refused with `invalid_config` and only `base64` works: the server's cwd is
not a safe default, since some MCP hosts start servers in `/` or `$HOME`.
Roots must be absolute (`~` is expanded), and `/`, the home directory and its
ancestors are refused; name the directory the media lives in. `pulsar serve`
prints the effective roots to stderr at startup.

## Operator commands

Beside `pulsar auth …` and `pulsar serve`, the CLI has the operator's verbs.
Each prints JSON and exits 0 on success, 1 otherwise.

| Command | Network | What it does |
|---|---|---|
| `pulsar status [--account A]` | none | per account: day and month budget, spent and remaining, posts today against the cap, quiet hours, and ledger keys still unresolved |
| `pulsar history [--account A] [--limit N]` | none | the newest ledger rows with their items |
| `pulsar validate PLAN.yaml [--account A]` | none | per account: the posts as they would go out, length, media facts, the plan digest and estimated cost; needs no credentials |
| `pulsar publish PLAN.yaml [--account A] [--idempotency-key K] [--caller C] --yes` | posts | publishes the plan to each of its accounts through the publisher; without `--yes` it only validates |
| `pulsar reconcile [--account A]` | reads the timeline only when something is unresolved | settles `unknown` and abandoned `submitting` posts (see [The ledger](#the-ledger)) |
| `pulsar import-posted FILE [--account A]` | `GET /2/users/me` only if the identity is not cached | imports a retired routine's `posted.jsonl` (idempotent) |

A **plan** is YAML (or, for `validate_plan`, the same shape as JSON):

```yaml
account: x:constworks            # or accounts: [...]; omitted = the default account
posts:                           # a thread; `text:` / `media:` at the top level is one post
  - text: "Orbit v0.26 is out"
    media: [{path: releases/v0.26/banner.png, alt: "The v0.26 banner"}]
  - text: "Notes: https://example.com/notes"
reply_to: "1790000000000000000"  # or quote: …; never both
variants:                        # per-provider replacement for posts
  bsky: {posts: [{text: "Shorter copy"}]}
not_before: 2026-10-01T16:00:00Z # refused with not_due (retryable) before then
```

Every media item needs `alt` text; paths resolve under `media.roots`. Text
and alt are posted as Unicode NFC with outer whitespace stripped. The
**digest** (`sha256:…`) covers the accounts, every post's text, every media
item's content hash and alt, reply/quote and variants. The media path and
`not_before` are left out, so renaming a file or moving the schedule keeps
the digest (and the idempotency key). A plan without accounts is bound to the
default account before it is digested. `publish` keys each account's row
`digest + account` unless `--idempotency-key` is given, which works for one
account only.

What `publish` does, in order:
1. Validate every post offline: provider rules (length, media type, size,
   count, video and GIF alone, alt text length), reply and quote ids, the
   secret scanner over every text and alt text, and media loaded under
   confinement.
2. Check the policy, and claim the ledger row, in one transaction. From here
   until each post is sent or the row settles, the plan's unsent posts count
   against the budget and daily cap, so a second plan cannot be admitted
   with what this one was admitted with.
3. Upload and post item by item; each reply goes to the previous item. A post
   is marked `submitting` before its media upload and re-stamped, as a
   compare-and-set, just before the post request leaves: if reconcile settled
   it meanwhile (a very slow upload looks like a dead sender), the post is not
   sent and the call reports that nothing was posted.

A definitive failure mid-thread leaves the row `partial`, and publishing
again resumes after the last published post. A post whose outcome is
ambiguous leaves the row `unknown` until `pulsar reconcile` settles it.

Reconcile lists the account's posts since just before the first ambiguous
send (up to 300; X bills post reads) and matches each ambiguous post by a
fingerprint of its text. The fingerprint ignores URLs (X rewrites them to
`t.co` and appends media links), the `@handles` X puts in front of a reply,
and whitespace; X's HTML entities are unescaped on X's copy only. A post
never matches a post id the ledger already holds. A post is marked absent
only when the listing was complete and five minutes have passed since it was
sent; otherwise it stays `unknown`. A post recorded before ledger v2 has no
fingerprint and is never marked absent: repeating the same `create_post`
(same text and key) answers `outcome_unknown` again but attaches one, and the
next reconcile can match it. Reconcile writes its verdicts for a row in one
transaction, and only if no sender touched the row since it was listed
(`state: changed` otherwise, and the exit code is 1).

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
| `whoami` | read-only | `GET /2/users/me` | optional `account`; cached; `{user_id, username}` of that account |
| `validate_post` | read-only | — | `text`, optional `reply_to_post_id`, `quote_post_id`; no network, no log |
| `validate_plan` | read-only | — | `plan` (the [plan](#operator-commands) as an object), optional `account`; per account `{account, digest, estimated_cost_usd, posts}`; no network, no log |
| `create_post` | publishes | `POST /2/tweets` | `text`, optional `reply_to_post_id`, `quote_post_id`, `media_ids`, `idempotency_key`, `dry_run` (legacy) |
| `upload_media` | publishes | `POST /2/media/upload/initialize` → `/{id}/append` → `/{id}/finalize`; `GET /2/media/upload` for video status | png/jpeg/gif/webp images ≤5 MiB or MP4 video (`video/mp4`) ≤100 MiB; `path` (a regular file inside `media.roots`) or `base64`, optional `mime` (must match the sniffed content) → `{media_id}` after video processing succeeds |
| `delete_post` | destructive | `DELETE /2/tweets/:id` | `post_id` (numeric X id), optional `idempotency_key` (default `delete:<post_id>`) → `{ok: true, post_id, deleted}` |

`validate_post` returns `{ok: true, text, weighted_length, has_url,
estimated_cost_usd}` without touching the network. `create_post` is a
one-post plan run through the publisher, so the policy applies to it
(`budget_exceeded`, `daily_cap` and `quiet_hours` come back before anything
is sent). It returns `{ok: true, post_id, url, text}`; `dry_run: true` returns what `validate_post`
does plus `dry_run: true`, and is kept for callers that predate `validate_post`;
a dry run is not a write and records nothing.
`whoami`, `create_post`, `upload_media` and `delete_post` take an optional
`account` (an alias such as `x:constworks`; default: `default_account`, else
the only bound account — see [Configuration](#configuration)); upload media
as the account that will post it. Before a write the account's bound handle
is checked (`account_mismatch`), and the ledger row records that account's
user id and handle.
Every writing tool takes an optional `caller` (agent id) for the ledger;
`PULSAR_CALLER` in the server's environment is the fallback. `caller` is
**advisory**: a self-asserted audit label, not an identity pulsar verifies.

#### Idempotency

Every post costs money and X has no idempotency key of its own, so pulsar
keeps one. `create_post` takes an optional `idempotency_key` (1–200
characters, no whitespace or control characters); without one, the key is
derived from the request (text, `reply_to_post_id`, `quote_post_id`,
`media_ids`) and the posting account's user id. The key is recorded in the
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

`create_post` keeps the request digest it had before plans existed, so
default keys and rows written by an older pulsar carry over unchanged.

Because of the derived key, posting the exact same text again from the same
account replays the first receipt. To post identical text deliberately (say,
after deleting the original), pass a fresh `idempotency_key`.

`delete_post` uses the same mechanism with the default key `delete:<post_id>`:
repeating a delete that succeeded returns `{ok: true, post_id, deleted,
replayed: true}` without calling X. `upload_media` records every upload in the
ledger but does not deduplicate: an orphaned media id is harmless and expires.

Plans and threads (`pulsar publish`) key one ledger row per plan and account,
with the same rules plus three more:

- **thread partly published** (some posts went out, a later one failed
  definitively) → the row is `partial`; calling again resumes after the last
  published post and never re-sends one.
- **key skipped** (a recorded decision never to publish it, e.g. imported
  from the old routine's `posted.jsonl`) → reported as skipped; nothing is
  sent. `create_post` with a skipped key is `idempotency_conflict` with
  `detail.state: skipped`, never a receipt.
- **key imported as published** from `posted.jsonl` → replays the imported
  receipt whatever the new plan's text, since the old routine kept no request
  to compare with.

Policy (budgets, daily cap) is checked in the same transaction that claims a
new row or re-arms a failed one, so a refused call leaves no row; a replay is
never re-checked. Re-armed posts take the current price and fingerprint.

#### `outcome_unknown`

Returned when the post request may have reached X but pulsar cannot tell
whether X created the post: the connection dropped or timed out after the
request was sent (`ReadTimeout`, `WriteTimeout`, `ReadError`,
`RemoteProtocolError`, …), X answered 5xx, or X answered 2xx without a
readable post id. The ledger row is left `unknown` and `detail` carries the
`cause` and the `idempotency_key`. **Do not retry blindly** — a retry that
succeeds is a second, paid post. Check the account's timeline; if the post is
not there and you still want it, call again with a *new* `idempotency_key`.
The operator's `pulsar reconcile` settles unknown rows against the account's
timeline; once a post is settled as absent, the same key re-sends it. Failures
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
| `auth_expired` | no account bound, the account was logged out, no token, or refresh failed (revoked / app reset; the account is then `reauth_required`), or legacy credentials that need `pulsar auth migrate` | stop; a human runs what `message` says (`pulsar auth login --account …`) |
| `account_mismatch` | the account's credentials belong to another handle than its alias's or the configured `expected_handle` (`detail: {alias, expected_handle, bound_handle}`); nothing was sent | stop; a human re-runs `pulsar auth login --account …` as the right account |
| `unknown_account` | `account` (or `default_account`) names an alias that is not registered; `detail.known` lists the ones that are | use a known alias, or have a human bind it |
| `insecure_storage` | the pulsar home or an account directory is wider than 0700, or `key` / `tokens.enc` / `accounts.json` wider than 0600 or not owned by the server's user | stop; a human runs the `chmod` in `message` (also `detail.fix`) — not a re-login |
| `invalid_config` | `config.toml` has an unknown key or a bad value (including a media root that is `/`, `~` or above it), `accounts.json` is unreadable, the ledger is from a newer pulsar, or a `path` upload with no `media.roots` configured | fix the file named in `message`; for uploads, pass `base64` or have the operator set roots |
| `invalid_text` | empty, over 280 weighted chars, control chars, reply+quote together | rewrite |
| `invalid_argument` | malformed `idempotency_key` or `account` alias, no `account` while several are bound and none is the default, or a `post_id` / `reply_to_post_id` / `quote_post_id` / `media_ids` entry that is not a 1–19 digit X id | fix the argument |
| `secret_detected` | text (or media, or `idempotency_key`) matches a credential pattern | rewrite; never retry verbatim |
| `invalid_media` | bad path/base64, path outside `media.roots` or not a regular file, content that is not png/jpeg/gif/webp/mp4 or does not match the declared/extension MIME (`detail: {declared, sniffed}`), oversized media, or failed/timed-out video processing | fix the input or inspect X's processing detail |
| `duplicate` / `forbidden` / `rate_limited` / `not_found` | X's reason, passed through in `detail` | duplicate: change text; rate_limited: wait |
| `idempotency_conflict` | the key was already used for a different request or account | use a new key |
| `budget_exceeded` / `daily_cap` / `quiet_hours` | the [policy](#configuration) refused the post; nothing was sent and no row was written. `detail.retry_after` says when it can pass | wait until `retry_after`; `retryable: false` means the post can never pass on its own (split it or raise the limit) |
| `invalid_plan` | a plan's shape is wrong (`detail.at` names where) | fix the plan |
| `not_due` | the plan's `not_before` is in the future (`detail.retry_after`) | publish after then |
| `unsupported` | the provider cannot do what the plan asks (a thread, reply or quote) | change the plan |
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
  MCP boundary in either direction. The `account` argument is an alias; it
  selects stored credentials, it never carries them.
- An account posts only as the handle it is named for. `auth login` checks
  the new token's owner with X before storing it, and every write re-checks
  the bound handle against the alias and `expected_handle`
  (`account_mismatch`, nothing sent). Aliases map to directory names by a
  strict, injective rule, so an alias cannot name a path outside `accounts/`.
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
- Encrypted-at-rest means Fernet with one key file for all accounts at the
  home root and each account's bundle in its own directory (files 0600,
  directories 0700). Saves are atomic (temp file, `fsync`, rename), so a crash
  mid-refresh never destroys the only refresh token, and concurrent first
  saves agree on one key. Every file pulsar creates in its home — `key`,
  `client.json`, `accounts.json`, `accounts.lock`, `writes.jsonl`, the
  ledger, and each `accounts/<slug>/tokens.enc` and `refresh.lock` — is 0600
  regardless of umask, and `accounts/` and every account directory are 0700.
- What that protects against: the bundle leaking in plaintext through
  backups, `cat`/`grep` over the home, a stray commit, or another local user.
  pulsar refuses to load or save credentials when the home or an account
  directory is wider than 0700, or `key`/`tokens.enc` wider than 0600 or
  owned by another uid
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
`provider`, the account's `account_alias` (`x:constworks`) and
`account_user_id`/`account_handle`, the advisory `caller`, `request_digest`
(SHA-256 of the canonical request), `plan_digest` (plan rows), `text_sha256`,
`state`, `post_id`/`media_id`/`url`, `error_code`/`error_message`/`retryable`,
`note` (why a key was skipped), `meta_json` (mime, bytes, processing_state,
deleted, superseded post), `attempts`, `created_at`/`updated_at`.

A plan row has one `items` row per post of its thread: `idx`, `state`,
`text_sha256`, `fingerprint` (for reconcile to match the post on the
provider), `est_cost_usd`, `post_id`/`url`, `media_ids_json`, the error
columns, and `submitted_at`. Legacy `create_post` rows mirror themselves as one
item, so every post counts toward the daily post cap. States:

```
legacy tools   submitting ──> published | failed | unknown

plan row       pending ──> submitting ──> published   every post confirmed
                                      ├─> partial     some published, the rest provably not
                                      ├─> failed      none published; a retry re-sends
                                      └─> unknown     a post may have gone out; blocked
               skipped                                decided never to publish this key
plan item      pending ──> submitting ──> published | failed | unknown
```

The row (or, for a thread, the item) is committed as `submitting` *before* its
request leaves, so a crash or kill mid-request leaves evidence, and that key
answers `outcome_unknown` until it is reconciled. Moving an item from
`pending` to `submitting` is a compare-and-set under the write lock, so two
callers holding the same pending row cannot both send a post; a thread's posts
start strictly in order. Calling again on a `failed` or `partial` row re-arms
its failed posts and keeps the published ones. Reconcile works on `unknown`
rows and on `submitting` rows whose newest post was submitted longer ago than
a staleness window of ten minutes (a sender that died; `submitted_at` is
re-stamped just before the post request, so a long media upload does not
count against it); it settles each ambiguous post as
published (with the id it found) or absent (`error_code:
outcome_resolved_absent`, re-sent on the next call), and the row's state is
derived again from its posts. Inspect it with `sqlite3 ledger.sqlite3 'select
* from writes'`.

**Usage** for policy: spend is the sum of `est_cost_usd` over posts that are
`submitting`, `published` or `unknown` (anything that may have cost money,
counted from when it was sent) plus the `pending` posts of a row still being
published (reserved, counted from when they were claimed), across all
accounts, since the start of the policy day and month; the daily post count
is the same posts for one account. Failed and skipped posts, and the unsent
posts of a row that has settled (`partial`, `failed`), are free. A row left
`pending` by a sender that died before its first post stays reserved until
the policy day ends or the same key is published again.

Rows from before ledger v2 and imported rows carry `est_cost_usd` 0: v1 kept
no text to price, so spend from before the upgrade is not counted against the
day or month budget (their posts do count toward the daily post cap).

**Schema versions.** v1 was the single-request ledger; v2 adds providers,
account aliases, plans and items. A v1 file migrates in place on first open
(`writes` is rebuilt to widen its states; old rows are recorded as provider
`x`, alias `x:<handle>`, and each `create_post` row gains its one item). A
pulsar older than the file refuses it with `invalid_config`.

**Importing `posted.jsonl`.** The retired x-updates routine's log
(`{key, ts, post_id|null, text?, note?, superseded_post_id?,
superseded_note?}` per line) imports with `pulsar import-posted FILE --account
x:<handle>` (`pulsar.core.importer.import_posted`):
a line with a `post_id` becomes a `published` row (tool `import:posted.jsonl`)
with one item carrying the post id, URL and the text's SHA-256 (never the
text), costing nothing; `post_id: null` becomes `skipped` with the line's note;
superseded facts go to `meta_json`. Timestamps are kept, normalised to UTC.
The import is idempotent, reports a key already taken by another write (or an
imported row that disagrees with its line) as a conflict without touching it,
and reports malformed lines by number without stopping. Imported rows are not
exported to `writes.jsonl`.

`writes.jsonl` beside it is an **export**, append-only: one line per terminal
transition (`published`, `partial`, `failed`, `unknown`, `skipped`) with `ts`,
`tool`, `caller`, `dry_run` (always false now), `post_id`, `text_sha256`,
`state`, `idempotency_key`, `account_user_id`, `error_code` on failure, and the
upload facts. Plan rows add `account_alias`, `plan_digest` and `items`
(`[{idx, state, post_id}]`); their `post_id` and `text_sha256` are the first
post's. Never post text, media bytes, or credentials.

## Development

```sh
make check     # ruff lint + format check + basedpyright strict + pytest (no network; fake X transport)
```

Layout:

```text
src/pulsar/core/          provider-neutral: plan, publisher, ledger, policy, channel contract
                          (adapter.py), account registry, credential store, media confinement,
                          secret scanner, settings, errors (no HTTP)
src/pulsar/providers/x/   X: channel adapter, OAuth 2.0 PKCE flow, v2 API client, text rules, limits
src/pulsar/surfaces/      front ends: mcp.py (standalone MCP server), cli.py, ops.py (operator verbs)
```

`tests/test_layering.py` enforces the boundaries on the import graph: `core`
imports no `httpx`, `mcp`, provider or surface; providers never import a
surface. `tests/conftest.py` holds the scripted X API; tests drive the server
through a real MCP client session over the in-memory transport.
