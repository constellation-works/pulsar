---
title: Publishing — Design
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: publishing
doc_role: design
type: design
summary: How a plan is normalised and digested, how the publisher admits, claims and sends it, and how reconcile settles unknown posts.
tags: [publishing, plan, ledger, idempotency, policy, reconcile]
paths: ["src/pulsar/core/plan.py", "src/pulsar/core/publisher.py", "src/pulsar/core/ledger/**", "src/pulsar/core/policy.py", "src/pulsar/core/usage.py", "src/pulsar/core/guard.py", "src/pulsar/core/media.py", "src/pulsar/core/importer.py", "src/pulsar/core/writelog.py"]
related_features: [accounts, channels, surfaces]
related_artifacts: [ORB-13027, ORB-13028, ORB-13030, ORB-13039]
---

# Publishing — Design

The publish path as built: plan, digest, publisher, policy, ledger, reconcile, import. The
invariants each part must keep are in [specs/](./specs/); approvals and dispatch (phase 4) are
in [3_vision.md](./3_vision.md).

## 1. Plans

A plan is YAML (the CLI) or the same shape as an object (MCP `validate_plan`, Orbit
`pulsar.validate`) ([plan.py](../../../src/pulsar/core/plan.py)):

```yaml
account: x:constworks            # or accounts: [...]; omitted = the default account
posts:                           # a thread; top-level text: / media: is one post
  - text: "Orbit v0.26 is out"
    media: [{path: releases/v0.26/banner.png, alt: "The v0.26 banner"}]
  - text: "Notes: https://example.com/notes"
reply_to: "1790000000000000000"  # or quote: …; never both
variants:                        # per-provider replacement for posts
  bsky: {posts: [{text: "Shorter copy"}]}
not_before: 2026-10-01T16:00:00Z # refused with not_due (retryable) before then
```

Unknown keys are `invalid_plan` with `detail.at`. Every media item needs `alt`. Text and alt
are Unicode NFC with outer whitespace stripped. A plan without accounts is bound to the default
account before it is digested.

The **digest** covers the accounts, every post's text, every media item's content hash and alt,
reply/quote and variants. The media path and `not_before` are excluded, so renaming a file or
moving the schedule keeps the digest and the idempotency key; see
[4_decisions.md](./4_decisions.md#the-digest-covers-content-not-where-or-when).

## 2. The Publisher

[publisher.py](../../../src/pulsar/core/publisher.py) runs a plan for one account in a fixed
order; the order is the safety argument.

1. **Prepare (offline).** Provider rules (length, media type, size, count, video and GIF
   alone, alt length), reply and quote ids, the secret scanner over every text and alt, media
   loaded under confinement. `Publisher.prepare(...).report()` is what `validate` returns: per
   account the posts as they would go out, weighted length, media facts, estimated cost and the
   digest.
2. **Admit and claim (one transaction).** The policy is checked against the ledger's usage and
   the row is claimed under SQLite's write lock (`BEGIN IMMEDIATE`). A refused call writes no
   row. From here until each post is sent or the row settles, the plan's unsent posts are
   reserved against the budget and cap.
3. **Send item by item.** Each post is marked `submitting` before its media upload and
   re-stamped by compare-and-set (`Ledger.item_sending`) just before the post request leaves. If
   reconcile settled it meanwhile (a very slow upload looks like a dead sender), the post is not
   sent. Each reply goes to the previous item.
4. **Finish.** Each outcome is committed as it happens; the row's state is derived from its
   items. `writes.jsonl` gets one line per terminal transition.

A definitive failure mid-thread leaves the row `partial`; publishing again resumes after the
last published post. A post whose outcome is ambiguous leaves the row `unknown`.

## 3. Idempotency

Summarised here; the contract is [specs/idempotency.md](./specs/idempotency.md).

- Plans key one row per account: `digest + account`, or an explicit key (one account only).
- `create_post` keeps its pre-plan request digest (text, reply, quote, media ids, account user
  id) so keys written by an older pulsar carry over.
- A repeat replays the receipt; a different request under the same key is
  `idempotency_conflict`; a definitive failure is retried; an unknown or in-flight key answers
  `outcome_unknown` again.
- `delete_post` keys `delete:<post_id>`. `upload_media` is recorded but not deduplicated: an
  orphaned media id is harmless and expires.

## 4. Policy

[policy.py](../../../src/pulsar/core/policy.py) checks, in order, `quiet_hours`, `daily_cap`
and `budget_exceeded` (day, then month) against [usage.py](../../../src/pulsar/core/usage.py),
before any network call, from `create_post` and `pulsar publish` alike. The rules, windows and
`retry_after` semantics are in [specs/policy.md](./specs/policy.md); the keys in
[references/config.md](./references/config.md). Prices come from `[prices.<provider>]` in
config, never constants.

## 5. The Ledger

`ledger.sqlite3` in the home, SQLite in WAL mode, schema version in `PRAGMA user_version`
([core/ledger/](../../../src/pulsar/core/ledger/)). One `writes` row per logical write and one
`items` row per post of a plan row. Several processes share it; claims and state changes take
the write lock. States, columns, usage accounting and schema migration are specified in
[specs/ledger.md](./specs/ledger.md).

`writes.jsonl` beside it is an append-only export for humans and old tooling
([writelog.py](../../../src/pulsar/core/writelog.py)): one line per terminal transition, with
`ts`, `tool`, `caller`, `post_id`, `text_sha256`, `state`, `idempotency_key`,
`account_user_id`, `error_code`, upload facts, and for plans `account_alias`, `plan_digest` and
`items`. Never text, media bytes or credentials.

## 6. Reconcile

`pulsar reconcile [--account A]` (`Publisher.reconcile`) settles `unknown` posts and
`submitting` posts older than the ten-minute staleness window (a sender that died).

1. List the account's posts since just before the first ambiguous send (up to 300; X bills
   post reads). Nothing is listed when nothing is unresolved.
2. Match each ambiguous post by the channel's text fingerprint
   ([Channels — Design](../channels/2_design.md)). A post never matches a post id the ledger
   already holds.
3. Mark it `published` with the id found, or absent (`outcome_resolved_absent`, re-sent on the
   next call) only when the listing was complete and five minutes have passed since it was
   sent. A post without a fingerprint (recorded before ledger v2) is never marked absent;
   repeating the same `create_post` attaches one.
4. Write the row's verdicts in one transaction (`Ledger.settle`), only if no sender touched the
   row since it was listed; otherwise report `state: changed` (exit 1).

## 7. Guards on Content

- **Secret scanner** ([guard.py](../../../src/pulsar/core/guard.py)): every post text, alt
  text, media payload and idempotency key is scanned for credential patterns (`sk-…`, `ghp_…`,
  `github_pat_…`, `xox…`, AWS/Google/Stripe keys, X bearer tokens, `Bearer …` headers, PEM
  private keys, JWTs, `token=…`-style assignments) before any network call, including on
  validation, and so is the caller label. A `token=…`-style assignment counts only when its
  value looks generated (at least 20 characters, at least 3.5 bits of entropy per character,
  not words joined by separators), so `password=correct-horse-battery-staple` in a post is
  prose, not a hit. The live values the process has loaded (each account's access and
  refresh token, the store key; 16 characters or more) are matched by value as `live
  credential`, since X's OAuth 2 tokens have no shape (STD-05 §R14). A hit is
  `secret_detected` naming the pattern, never the match. It is a backstop: most secrets
  match no pattern.
- **Redaction at rest** (`guard.redact`): free text pulsar persists or logs (the caller label,
  provider error messages, notes, `meta` strings, `writes.jsonl` lines, stderr logs) passes
  through `redact`, which masks the same live values and shapes as `[redacted:<label>]` and keeps the words
  around them (STD-05 §R13). The ledger's inventory is `ledger/text.py`'s `REDACTED_COLUMNS`.
- **Media confinement** ([media.py](../../../src/pulsar/core/media.py)): files are read only
  from configured roots, opened without following symlinks, size-checked before reading, and
  typed by content. See [specs/media-confinement.md](./specs/media-confinement.md).

## 8. Importing `posted.jsonl`

The retired x-updates routine kept `{key, ts, post_id|null, text?, note?,
superseded_post_id?, superseded_note?}` per line.
`pulsar import-posted FILE --account x:<handle>` ([importer.py](../../../src/pulsar/core/importer.py)):

- a line with a `post_id` becomes a `published` row (tool `import:posted.jsonl`) with one item
  carrying the post id, URL and the text's SHA-256 (never the text), costing nothing;
- `post_id: null` becomes `skipped` with the line's note;
- superseded facts go to `meta_json`; timestamps are kept, normalised to UTC.

The import is idempotent, reports a key held by another write (or an imported row that
disagrees with its line) as a conflict without touching it, reports malformed lines by number
and carries on. Imported rows are not exported to `writes.jsonl`. A replay of an imported
published key returns the imported receipt whatever the new text: the routine kept no request
to compare.

## 9. Concerns & Honest Limitations

- **Unknown can last forever.** Reconcile refuses to call a post absent without a complete
  listing and a grace period, and never for a post without a fingerprint. That favours "stuck"
  over "posted twice" by design, and needs a human when X's listing is incomplete.
- **A dead sender's reservation outlives it.** A row left `pending` before its first post keeps
  its posts reserved against the budget and cap until the policy day ends or the same key is
  published again.
- **Pre-v2 and imported spend is invisible to budgets.** Those rows carry cost 0 (v1 kept no
  text to price); their posts still count toward the daily cap.
- **Estimated, not billed, cost.** Budgets use the configured price table. If X changes prices
  and the config is not updated, budgets are wrong in either direction.
- **Blocking I/O on the event loop.** File and SQLite calls run on the async path;
  [ORB-13039] proposes moving them off it, together with closing a media-root swap race.
- **The fingerprint can collide.** Two posts that differ only in URLs, leading mentions or
  whitespace fingerprint alike; the "never match an id already held" rule limits the damage to
  mis-attributing which of two ambiguous posts went out.

## Task References

- [ORB-13027] — added the ledger v0, idempotency and `outcome_unknown`.
- [ORB-13028] — added plans, the publisher, ledger v2, policy, reconcile, import.
- [ORB-13030] — phase 4: approvals, standing policies, dispatch.
- [ORB-13039] — proposed: blocking I/O off the event loop; media-root swap race.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
