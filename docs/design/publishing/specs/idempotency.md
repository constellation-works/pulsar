---
type: design
summary: "Spec: idempotency keys — derivation, replay, conflict, retry and outcome_unknown"
last_validated: 2026-10-03
---

# Spec: Idempotency

Every write has an idempotency key recorded in the [ledger](./ledger.md) before the request is
sent. Repeating a call with the same key never produces a second post: it replays the receipt,
reports a conflict, resumes a partial thread, or refuses while the first attempt's outcome is
unknown.

## Why This Exists

Harnesses and routines retry. The provider has no idempotency key of its own, so without this a
retry after a timeout is a second paid post.

## Keys

- 1–200 characters, no whitespace or control characters; scanned by the secret scanner.
- **Plans** (`pulsar publish`, `pulsar.publish`): one row per account, key `digest + account`
  by default. An explicit key works for one account only: the plan's own `key` (digested, so
  the approval covers it; `invalid_plan` with two accounts), or `--idempotency-key` on the CLI
  (`invalid_argument` with several accounts, or when it differs from the plan's `key`).
  Routines put their own in the plan (`release:<repo>:<tag>`, `repo:<name>`, `pr:<repo>:<n>`).
- **`create_post`**: default key derived from the request (text, `reply_to_post_id`,
  `quote_post_id`, `media_ids`) and the posting account's user id, using the pre-plan request
  digest so keys from an older pulsar carry over.
- **`delete_post`**: default `delete:<post_id>`.
- **`upload_media`**: recorded, not deduplicated.

## Outcomes of a Repeat

| Stored row | Repeat with the same key |
|---|---|
| published | the stored receipt, `replayed: true`; nothing is sent |
| different request, tool or account | `idempotency_conflict`; nothing is sent |
| failed definitively (never reached the provider, or rejected) | retried |
| partial thread | resumes after the last published post; never re-sends one |
| unknown, or still in flight | `outcome_unknown` again; nothing is sent |
| skipped | plans: the skipped receipt, `replayed: true`, including imported skips with no items; `create_post`: `idempotency_conflict` with `detail.state: skipped` |
| imported as published | the imported receipt, whatever the new text (its `digest` is null) |

Policy is checked in the same transaction that claims a new row or re-arms a failed one; a
refused call leaves no row, and a replay is never re-checked.
Plan preflight also resolves published and skipped rows before approval and policy checks:
quiet hours, the daily cap and budget limits cannot prevent a terminal receipt from replaying.
The ledger claim still verifies that the key belongs to this account and checks request
conflicts under its write lock before returning a receipt.

## `outcome_unknown`

Returned when the request may have reached the provider but pulsar cannot tell whether it
created the post:

- the connection dropped or timed out after the request was sent (`ReadTimeout`,
  `WriteTimeout`, `ReadError`, `RemoteProtocolError`, …);
- the provider answered 5xx;
- the provider answered 2xx without a readable post id.

The row is left `unknown`; `detail` carries the `cause` and the key; `retryable` is false.
Callers must not retry. Failures that prove nothing was sent (`ConnectError`,
`ConnectTimeout`, `PoolTimeout`) are `api_error` with `retryable: true`.

Resolution: `pulsar reconcile` settles the row against the account's timeline; once a post is
settled absent, the same key re-sends it. A caller that has checked the timeline itself and
wants the post may use a new key.

## Posting Identical Text Deliberately

Because the default key is derived from the request, the same text from the same account
replays the first receipt. To post it again (after deleting the original), pass a fresh key.
