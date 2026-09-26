---
type: design
summary: "Spec: idempotency keys — derivation, replay, conflict, retry and outcome_unknown"
last_validated: 2026-09-26
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
- **Plans** (`pulsar publish`): one row per account, key `digest + account` by default. An
  explicit key works for one account only (`invalid_argument` otherwise). Routines pass their
  own (`release:<repo>:<tag>`, `pr:<repo>:<n>`).
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
| skipped | plans: reported as skipped; `create_post`: `idempotency_conflict` with `detail.state: skipped` |
| imported as published | the imported receipt, whatever the new text |

Policy is checked in the same transaction that claims a new row or re-arms a failed one; a
refused call leaves no row, and a replay is never re-checked.

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
