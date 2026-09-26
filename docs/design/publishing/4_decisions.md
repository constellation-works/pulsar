---
title: Publishing — Decisions
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: publishing
doc_role: decisions
type: design
summary: Record before send, a missed post beats a double post, digest excludes path and schedule, reservations, legacy digests.
tags: [publishing, ledger, idempotency, policy]
paths: ["src/pulsar/core/ledger/**", "src/pulsar/core/publisher.py", "src/pulsar/core/plan.py"]
related_features: [channels, surfaces]
related_artifacts: [ORB-13027, ORB-13028]
---

# Publishing — Decisions

Non-obvious decisions about the publish path. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## Record every write before it leaves

**Recorded:** 2026-09-26 · [ORB-13027]

### Context

The v1 write log appended a line after a successful post. A crash, timeout or kill between the
request and the log line left no trace, and the retry posted again.

### Decision

Every write, on every surface and every provider, is committed to the ledger before its
network request, and every later fact is committed as it happens. There is no bypass flag, and
no new write path may skip it.

### Consequences

- Any post pulsar may have made has a row, so it can be found, replayed or reconciled.
- Cost: every write pays a SQLite transaction before and after the call, and a crash leaves
  `submitting` rows that block their key until reconcile runs.

## A missed post beats a double post

**Recorded:** 2026-09-26 · [ORB-13027]

### Context

After an ambiguous failure pulsar cannot tell whether the provider created the post. Retrying
risks a duplicate paid post in public; not retrying risks a missing one.

### Decision

When pulsar cannot prove a post did not go out, it assumes it may have: the key answers
`outcome_unknown`, callers are told not to retry, and reconcile marks a post absent only with a
complete listing, a grace period and a fingerprint. New mechanisms facing the same ambiguity
choose the same way.

### Consequences

- Duplicates require two separate human decisions, never one retry.
- Cost: rows can stay `unknown` indefinitely, and a routine can silently skip a post until a
  human reconciles.

## The digest covers content, not where or when

**Recorded:** 2026-09-26 · [ORB-13028]
**Code anchors:** `src/pulsar/core/plan.py::Plan`

### Context

The digest is the approval key and the default idempotency key. Media is referenced by path
and plans carry a `not_before` schedule.

### Decision

The digest covers accounts, text, media content hashes and alt text, reply/quote and variants.
It excludes media paths and `not_before`.

### Consequences

- Renaming an image or rescheduling a post keeps its approval and its key.
- Cost: two plans that differ only in schedule share a key, so publishing the second replays the
  first's receipt; a deliberate repeat needs an explicit key.

## Reserve a plan's unsent posts until it settles

**Recorded:** 2026-09-26 · [ORB-13028]
**Code anchors:** `src/pulsar/core/ledger/queries.py::usage`

### Context

A thread is admitted against the budget and cap, then posted one item at a time. Counting only
sent posts let a second plan be admitted with the headroom the first had already used.

### Decision

Usage counts the `pending` items of rows still being published (reserved from when they were
claimed) as well as sent, published and unknown ones.

### Consequences

- Concurrent admissions cannot overspend.
- Cost: a sender that dies before its first post holds its reservation until the policy day
  ends or the key is published again.

## `create_post` keeps its original request digest

**Recorded:** 2026-09-26 · [ORB-13028]
**Code anchors:** `src/pulsar/core/ledger/keys.py::request_digest`, `src/pulsar/mcp.py::_legacy_post`

### Context

`create_post` became a one-post plan run through the publisher. Its keys and rows predate
plans; a new digest would give every existing default key a new identity.

### Decision

`create_post` derives its default key from the v1 request digest (text, reply, quote, media ids,
account user id), not the plan digest.

### Consequences

- Rows written by an older pulsar replay and conflict exactly as before.
- Cost: two digest schemes coexist in the ledger, and `create_post` and `pulsar publish` of the
  same text do not share a key.

## X's duplicate refusal is classified from its text

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/providers/x/client.py`

### Context

X answers a duplicate post with 403 (sometimes 400) and a `detail` saying it is a duplicate; the
response carries no structured code that tells it from other 403s (suspension, a missing
scope). pulsar reports duplicates as `duplicate`, which callers treat as "already said".

### Decision

The X client matches "duplicate" in a 403 or 400 body, the one place pulsar classifies by
substring; everything else is classified from status codes and fields.

### Consequences

- A repeated post is reported as a duplicate, not a permission failure.
- Cost: if X rewords the message, duplicates become `forbidden`; the client tests pin the
  current wording so the change shows up as a failing test, not a silent reclassification.

## Publisher ledger writes run on the event loop

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/core/publisher.py::Publisher.publish`, `src/pulsar/core/publisher.py::Publisher.reconcile`, `src/pulsar/mcp.py::_settle_on_error`, `src/pulsar/mcp.py::_record_success`

### Context

Each post is claimed and marked `submitting` in SQLite before its network call, and its
outcome recorded right after. Handing those writes to a thread lets a cancellation detach the
write from the send: the task can be cancelled after the send while the thread still commits,
or before the commit while the send already happened.

### Decision

The publisher's per-item ledger writes (short, local transactions under a 5 s busy timeout),
the MCP server's settling of a legacy `upload_media` / `delete_post` row, and reconcile's
ledger reads and settles between its timeline requests, run inline on the event loop, so
each is ordered with its send or read. Slow or unbounded work (preparing a plan, reading
media, account and registry reads, claims made by the MCP surfaces) goes to a thread on
every surface, the CLI included.

### Consequences

- The ledger can never disagree with what was sent because of task cancellation.
- Cost: under lock contention a write can hold the loop up to the busy timeout; the MCP
  server serves one account's writes at a time anyway.

## `writes.jsonl` is a best-effort export

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/core/writelog.py::WriteLog.export`

### Context

The ledger (SQLite) is the one commit decision for every write. `writes.jsonl` is a
line-per-row export for tools that tail a file; it is appended after the ledger commits.

### Decision

The export is written after the commit and a failure to write it is logged, not raised: the
write already happened, and reporting it as failed would invite a duplicate.

### Consequences

- The ledger, `pulsar history` and `pulsar.history` are always right.
- Cost: `writes.jsonl` can miss a row after a disk error; it is not a source of truth and
  nothing reads it back.

## A stale delete is re-armed and a lost delete is retryable

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/core/ledger/records.py::REARMABLE_TOOLS`, `src/pulsar/core/ledger/facade.py::Ledger.claim`, `src/pulsar/mcp.py::delete_post`, `src/pulsar/providers/x/client.py::XClient._post_refresh`

### Context

Work a silent peer holds is not reclaimed because a TTL lapsed; only an explicit, recorded
reclaim may take it. A `delete_post` row stays `submitting` when its sender dies
mid-request, and its key (default `delete:<post_id>`) would then refuse every later delete of
that post with `outcome_unknown`, with nothing to reconcile: legacy rows have no items.

### Decision

`claim(stale_after=STALE_SUBMITTING)` (ten minutes) re-arms a `submitting` row, but only for
`REARMABLE_TOOLS`, the requests with no duplicate effect: `delete_post` (DELETE is idempotent
at X) and `upload_media` (a second upload leaves an orphaned media id that expires; uploads
also take a fresh key per call). The takeover bumps `attempts` and writes a `note`, so it is
recorded. `create_post` and plan rows are never re-armed; they go through `pulsar reconcile`.

For the same two requests a reply lost after sending is reported as a retryable `api_error`,
not `outcome_unknown` (`ambiguous=False` in `mcp.py`): repeating either is harmless, so the
caller is told to retry rather than to reconcile. A lost token-refresh reply is a retryable
`api_error` whose message says X may have rotated the pair (`detail.outcome: "unknown"`).

### Consequences

- A crashed delete does not block that post's deletion for good.
- Cost: an automatic reclaim after a TTL, which pulsar avoids in general; confined to requests
  whose repeat is harmless, and tests pin that `create_post` is never taken over.

## Protocol strings are validated at the edge, not wrapped in types

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/core/ledger/keys.py::check_key`, `src/pulsar/providers/x/client.py::check_x_id`, `src/pulsar/core/plan.py::alias_provider`

### Context

Newtypes with validating constructors for ids, keys and selectors are an alternative to
bare strings checked by helpers. pulsar's are idempotency keys, account aliases, X post and
media ids, and digests.

### Decision

Each is validated once where it enters (a tool argument, a plan, the ledger) by a `check_*`
helper or by `Plan.from_mapping`, and flows on as `str`. Records that hold them (`AccountRef`,
`PlanRecord`, `WriteRecord`) are frozen dataclasses built only after validation.

### Consequences

- The validation is in one place per kind, and the JSON and SQLite boundaries stay plain.
- Cost: the type checker cannot tell a validated key from any string; a new entry point must
  call the helper. Revisit if a second provider adds id shapes.

## Task References

- [ORB-13027] — added the ledger v0 and `outcome_unknown`.
- [ORB-13028] — added plans, digests, reservations and the publisher.
- [ORB-13138] — aligned the publishing core with the constellation standards.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
