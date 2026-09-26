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
paths: ["src/pulsar/core/ledger.py", "src/pulsar/core/publisher.py", "src/pulsar/core/plan.py"]
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
**Code anchors:** `src/pulsar/core/ledger.py::Ledger._usage`

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
**Code anchors:** `src/pulsar/core/ledger.py::request_digest`, `src/pulsar/surfaces/mcp.py::_legacy_post`

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

## Task References

- [ORB-13027] — added the ledger v0 and `outcome_unknown`.
- [ORB-13028] — added plans, digests, reservations and the publisher.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
