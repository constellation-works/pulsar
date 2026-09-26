---
type: design
summary: "Spec: the write ledger — rows, items, states, usage, schema versions and read-only opens"
paths: ["src/pulsar/ledger/**"]
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
  The code is the package `src/pulsar/ledger/` (`records`, `keys`, `text`, `schema`,
  `connection`, `queries`, `single`, `plans`, `imports`, `facade`); import it as
  `pulsar.ledger`.
- Several processes share one file. Every claim and state change runs in `BEGIN IMMEDIATE`
  with a busy timeout, so two processes cannot claim or send the same key. A lock is recognised
  by SQLite's result code (`SQLITE_BUSY` / `SQLITE_LOCKED`), never by message text.
- Every connection reads `user_version`. A file newer than this pulsar is refused with
  `invalid_config` naming the path and both versions, including in a long-running process after
  a newer pulsar migrated the file under it; writes re-check it under the write lock. Pulsar
  never downgrades a file.
- A read-write open migrates an older file on first use. `Ledger.migrate()` (the `pulsar
  migrate` verb) does it explicitly and returns `(from_version, to_version)`.

## Read-Only Opens

`SqliteLedger(paths, read_only=True)` serves reports.

- It never creates the home or the file, never switches the journal mode, never migrates and
  never takes a write lock. Every state change on it raises `internal`.
- A missing file (or one no schema was ever applied to) reads as empty: no rows, zero usage.
- A file older than this pulsar is refused with `invalid_config`, naming the path and `pulsar
  migrate`. A newer file is refused as above.
- It opens `mode=ro`. In a home it cannot write, SQLite cannot create the WAL index (`-shm`):
  - with no `-wal` file the main file holds every committed write, so it is read with
    `immutable=1`, and read again if its inode, size or mtime changed meanwhile;
  - with a `-wal` file (committed writes may be only there) the read is refused with
    `invalid_config` and the remedy (make the home writable). It never reads around the log.
- In a writable home SQLite may leave its empty `-wal`/`-shm` pair after a read-only open. That
  is lock state, not ledger state; the next writer removes it.

## Rows (`writes`)

One row per logical write: `idempotency_key` (unique), `tool`, `provider`, `account_alias`,
`account_user_id`, `account_handle`, `caller` (advisory), `request_digest` (SHA-256 of the
canonical request), `plan_digest` (plan rows), `text_sha256`, `state`, `post_id` / `media_id`
/ `url`, `error_code` / `error_message` / `retryable`, `note` (why a key was skipped, or that a
stale claim was taken over), `meta_json` (mime, bytes, processing state, deleted, superseded
post), `attempts`, `created_at` / `updated_at`.

## Items (`items`)

One per post of a plan row's thread: `idx`, `state`, `text_sha256`, `fingerprint`,
`est_cost_usd`, `post_id` / `url`, `media_ids_json`, the error columns, `submitted_at`. A legacy
`create_post` row mirrors itself as one item, so every post counts toward the daily cap.

## Free Text

`writes.caller`, `writes.error_message`, `writes.note`, the strings in `writes.meta_json` and
`items.error_message` are written only through one redaction hook (`text.persisted_text`);
`text.REDACTED_COLUMNS` is the inventory and a test checks it against the schema and every
writer. Every other TEXT column is structured (keys, digests, ids, codes, states, times).

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

States are the `State` enum; the stored and exported values are the strings above. A row is
*settled* (`is_settled`) when it is `published`, `partial`, `failed` or `skipped`; `pending`,
`submitting` and `unknown` are not. Reconcile's per-row result `changed` is not a ledger state.

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
   are unchanged since it listed them; a verdict for an item that is not open is refused.
8. A `delete_post` or `upload_media` row left `submitting` is taken over by a claim that passes
   `stale_after` once its `updated_at` is that old: `attempts` goes up and `note` records it.
   Neither request has a duplicate effect (DELETE is idempotent at X; a second upload leaves an
   orphaned media id). A `create_post` row is never taken over.
9. A broken ledger invariant (a transition from the wrong state, a missing row for a claimed
   key, a failed compare-and-set) is `internal`; a bad argument (an item index the row does
   not have, a naive timestamp, a plan with no posts) is `invalid_argument`.

## Usage and Queries

- **Spend**: the sum of `est_cost_usd` over items that are `submitting`, `published` or
  `unknown` (counted from `submitted_at`), plus the `pending` items of rows still being
  published (reserved, counted from when they were claimed), across all accounts, since the
  start of the policy day and month.
- **Posts today**: the same items, for one account.
- Free: `failed` and `skipped` items, and the unsent items of a settled row (`partial`,
  `failed`).
- The row being admitted is excluded from its own usage.
- `history(limit, account_alias)` lists rows newest first; `count(account_alias)` is the total
  it would match without the limit. `last_published(alias)` is the account's newest `published`
  row, filtered in SQL before the limit.

## Schema Versions

- **v1**: single-request rows (`create_post`, `upload_media`, `delete_post`).
- **v2**: providers, aliases, plans and items. A v1 file migrates in place: `writes` is rebuilt
  to widen its states, old rows get provider `x` and alias `x:<handle>`, and each
  `create_post` row gains its one item (cost 0, no fingerprint).
- Shipped migrations are never edited; a change is a new one appended to `schema.MIGRATIONS`.
  A new file and one migrated from v1 end with identical schemas (tested).

## Failure Modes

- Process killed after commit, before send: the item is `submitting`; reconcile treats it as
  stale after ten minutes and settles it against the timeline.
- Process killed after send, before commit: the item is `submitting` or `unknown`; the same.
- A delete or upload killed mid-request: its row (no items, so reconcile never sees it) stays
  `submitting` until a claim with `stale_after` takes it over (invariant 8).
- Two processes claim the same key: one waits on the write lock, then sees the claimed row and
  answers `outcome_unknown` (in flight) or the receipt.
- A newer pulsar migrates the file while an older one runs: the older one's next read or write
  is refused; it writes nothing.
- Disk full, or the database locked past the busy timeout: the claim fails and nothing is
  sent.
