---
type: design
summary: "Spec: the write ledger — rows, items, states, usage and schema versions"
last_validated: 2026-09-26
---

# Spec: The Write Ledger

`ledger.sqlite3` in the pulsar home is the source of truth for every write. A write is
recorded, and committed, before its request leaves the host; every later fact about it (sent,
published, failed, unknown, settled) is committed as it happens. Nothing else — not
`writes.jsonl`, not a provider's timeline — is authoritative for what pulsar did.

## Why This Exists

A post costs money and the provider cannot deduplicate it. A crash, a timeout or a retry
between "decided to post" and "know whether it posted" must leave evidence that blocks a second
paid post and lets a human or reconcile settle the first.

## Storage

- SQLite, WAL mode, created 0600 in the 0700 home; schema version in `PRAGMA user_version`.
- Several processes share one file. Every claim and state change runs in `BEGIN IMMEDIATE`
  with a busy timeout, so two processes cannot claim or send the same key.
- A pulsar older than the file refuses it with `invalid_config`; it never downgrades.

## Rows (`writes`)

One row per logical write: `idempotency_key` (unique), `tool`, `provider`, `account_alias`,
`account_user_id`, `account_handle`, `caller` (advisory), `request_digest` (SHA-256 of the
canonical request), `plan_digest` (plan rows), `text_sha256`, `state`, `post_id` / `media_id`
/ `url`, `error_code` / `error_message` / `retryable`, `note` (why a key was skipped),
`meta_json` (mime, bytes, processing state, deleted, superseded post), `attempts`,
`created_at` / `updated_at`.

## Items (`items`)

One per post of a plan row's thread: `idx`, `state`, `text_sha256`, `fingerprint`,
`est_cost_usd`, `post_id` / `url`, `media_ids_json`, the error columns, `submitted_at`. A legacy
`create_post` row mirrors itself as one item, so every post counts toward the daily cap.

## States

```
legacy tools   submitting ──> published | failed | unknown

plan row       pending ──> submitting ──> published   every post confirmed
                                      ├─> partial     some published, the rest provably not
                                      ├─> failed      none published; a retry re-sends
                                      └─> unknown     a post may have gone out; blocked
               skipped                                decided never to publish this key
plan item      pending ──> submitting ──> published | failed | unknown
```

Invariants:

1. A row (or item) is committed `submitting` before its request is sent.
2. `pending → submitting` for an item is a compare-and-set under the write lock; a thread's
   items start strictly in order, and two callers holding the same pending row cannot both send.
3. `submitted_at` is re-stamped by compare-and-set just before the post request, after any
   media upload. If the stamp no longer matches (reconcile settled the item), the post is not
   sent.
4. A plan row's state is always derived from its items (`derive_state`), never set directly.
5. Calling again on a `failed` or `partial` row re-arms its failed items with the current
   text hash, fingerprint and price, and keeps the published ones.
6. `unknown` is left only by reconcile or an operator; no caller retry moves it.
7. Reconcile writes all verdicts for a row in one transaction, and only if the row's open items
   are unchanged since it listed them.

## Usage (for policy)

- **Spend**: the sum of `est_cost_usd` over items that are `submitting`, `published` or
  `unknown` (counted from `submitted_at`), plus the `pending` items of rows still being
  published (reserved, counted from when they were claimed), across all accounts, since the
  start of the policy day and month.
- **Posts today**: the same items, for one account.
- Free: `failed` and `skipped` items, and the unsent items of a settled row (`partial`,
  `failed`).
- The row being admitted is excluded from its own usage.

## Schema Versions

- **v1**: single-request rows (`create_post`, `upload_media`, `delete_post`).
- **v2**: providers, aliases, plans and items. A v1 file migrates in place on first open:
  `writes` is rebuilt to widen its states, old rows get provider `x` and alias `x:<handle>`,
  and each `create_post` row gains its one item (cost 0, no fingerprint).

## Failure Modes

- Process killed after commit, before send: the item is `submitting`; reconcile treats it as
  stale after ten minutes and settles it against the timeline.
- Process killed after send, before commit: the item is `submitting` or `unknown`; the same.
- Two processes claim the same key: one waits on the write lock, then sees the claimed row and
  answers `outcome_unknown` (in flight) or the receipt.
- Disk full, or the database locked past the busy timeout: the claim fails and nothing is
  sent.
