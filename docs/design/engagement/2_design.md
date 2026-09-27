---
title: Engagement — Design
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Draft
feature: engagement
doc_role: design
type: design
summary: How a read is budgeted, made and recorded, and how a mention is known to be answered.
tags: [engagement, reads, mentions, metrics, budget, ledger]
paths: ["src/pulsar/app/core/engagement/**", "src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/ledger/reads.py", "src/pulsar/app/core/ledger/queries.py"]
related_features: [publishing, channels, surfaces]
related_artifacts: [ORB-13030]
---

# Engagement — Design

The engagement loop as built: reads through the `Reader`. The provider side of each read is in
[Channels — Design §7](../channels/2_design.md#7-reads); the ledger rows are in the
[ledger spec](../publishing/specs/ledger.md#reads-reads).

## 1. Reads

[reader.py](../../../src/pulsar/app/core/engagement/reader.py) `Reader` makes two reads for a
bound account (identity checked, like a write):

| Read | Returns | Channel call |
|---|---|---|
| `mentions(bound, since, max_posts, caller)` | others' posts mentioning the account, and which of them it has answered | `Channel.mentions` |
| `own_posts(bound, since, max_posts, caller)` | the account's posts with their metrics | `Channel.own_posts` |

Each read runs in the publisher's order, for a call that changes nothing at the provider:

1. **Admit.** The most the read can cost, `max_posts` at the provider's `read_post_usd`, is
   checked against the day's and month's budgets (`Policy.check_read`). Quiet hours and the
   daily post cap do not apply to reads.
2. **Read** through the account's channel.
3. **Record** a `reads` row: the kind, account, window, the number of posts the provider
   returned (and billed) and their cost. From then on it counts toward the same spend as posts.
   A read that fails records nothing.

What a read returns goes back to the caller and nowhere else
([decision](./4_decisions.md#pulsar-never-stores-what-it-reads)). The account's own posts that
mention it (its side of a conversation) are dropped from `mentions`, though they were billed.

**Answered mentions.** Since ledger v3 every claimed item records the post it replies to
(`items.reply_to`, a thread's first item only). A mention is *answered* when the account has a
reply to it that went out or may have (`submitting`, `published`, `unknown`); a failed reply
answers nothing, and neither does a reply made outside pulsar.

## 2. Concerns & Honest Limitations

- **The read price is an estimate.** `read_post_usd` defaults to $0.005 a post until checked on
  the X developer portal; X may also bill the author records a mentions read expands.
- **Concurrent reads can overshoot.** Two reads can both pass the budget check before either
  is recorded; the next check sees both.
- **Re-reading costs.** Overlapping windows re-read, and pay for, the same mentions, because
  pulsar keeps no inbox.
- **A read killed mid-call is unrecorded.** Pages already fetched were billed but have no row.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch; approvals land here.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
