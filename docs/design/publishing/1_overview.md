---
title: Publishing — Overview
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: publishing
doc_role: overview
type: design
summary: The plan, its digest, the publisher, the policy and the ledger — how pulsar turns a request into at most one paid post per intent.
tags: [publishing, plan, ledger, idempotency, policy, reconcile]
paths: ["src/pulsar/plan/model.py", "src/pulsar/publishing/publisher.py", "src/pulsar/ledger/**", "src/pulsar/publishing/policy.py", "src/pulsar/ledger/usage.py", "src/pulsar/guard/scanner.py", "src/pulsar/publishing/media.py", "src/pulsar/app/importer.py", "src/pulsar/app/writelog.py", "src/pulsar/home/settings.py"]
related_features: [accounts, channels, surfaces]
related_artifacts: [ORB-13006, ORB-13027, ORB-13028, ORB-13030]
---

# Publishing — Overview

Every post pulsar makes goes through one publisher: validate offline, check the policy, claim
a ledger row, then send one recorded step at a time. The ledger is written before anything
leaves the host, keyed by an idempotency key, so a retry replays a receipt instead of paying
for a second post, and a post whose outcome is unknown is held until it is reconciled against
the account's timeline.

## 1. Motivation

- **Posts cost money and X has no idempotency key.** About $0.015 a post, $0.20 with a URL. A
  client retry after a timeout is a second paid post unless the connector remembers the first.
- **Agents retry.** Harnesses re-run tool calls after errors and restarts; a routine re-runs
  every day. The same intent must not become two posts.
- **Threads fail halfway.** Post 3 of 5 can fail after posts 1 and 2 went out; the retry must
  resume, not repeat.
- **Spend needs a ceiling.** An agent loop can post until the budget is gone. Budgets and a
  daily cap are checked before the network, from what the ledger has committed.
- **Approval needs something to bind to.** A human approves exact content; the plan's digest
  is that content's identity (approvals themselves arrive in phase 4, [ORB-13030]).

## 2. Core Concepts

- **Plan.** A provider-neutral description of what to publish: accounts, posts (a thread;
  one post is a thread of one) with media and alt text, reply or quote target, per-provider
  variants, `not_before`.
- **Digest.** `sha256:…` over the plan's canonical content; the approval and idempotency key.
- **Row and items.** One ledger row per plan and account; one item per post of its thread.
- **Idempotency key.** The row's unique key: the digest plus account by default, or a caller's
  own (`pr:nebula:54`).
- **Receipt.** What a finished row answers on a repeat: post ids and URLs, `replayed: true`.
- **Unknown.** A post that may have reached the provider; blocks its key until reconciled.
- **Reconcile.** Matching unknown posts against the account's recent posts by text
  fingerprint.
- **Policy.** Quiet hours, per-account daily post cap, day and month USD budgets.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| Plan model, normalisation, canonical digest | [plan/model.py](../../../src/pulsar/plan/model.py) | [ORB-13028] |
| Validate, admit, claim, send, reconcile | [publishing/publisher.py](../../../src/pulsar/publishing/publisher.py) | [ORB-13028] |
| Ledger: rows, items, states, usage, schema versions | [ledger/](../../../src/pulsar/ledger/) | [ORB-13027], [ORB-13028] |
| Policy: quiet hours, cap, budgets | [publishing/policy.py](../../../src/pulsar/publishing/policy.py), [ledger/usage.py](../../../src/pulsar/ledger/usage.py) | [ORB-13028] |
| Secret scanner | [guard/scanner.py](../../../src/pulsar/guard/scanner.py) | [ORB-12124] |
| Media confinement, type sniffing, limits | [publishing/media.py](../../../src/pulsar/publishing/media.py) | [ORB-13006], [ORB-13027] |
| `writes.jsonl` export | [app/writelog.py](../../../src/pulsar/app/writelog.py) | [ORB-13027] |
| `posted.jsonl` import | [app/importer.py](../../../src/pulsar/app/importer.py) | [ORB-13028] |
| `config.toml` | [home/settings.py](../../../src/pulsar/home/settings.py) | [ORB-13027], [ORB-13028] |

Contracts: [specs/ledger.md](./specs/ledger.md), [specs/idempotency.md](./specs/idempotency.md),
[specs/policy.md](./specs/policy.md), [specs/media-confinement.md](./specs/media-confinement.md).
Lookups: [references/config.md](./references/config.md),
[references/glossary.md](./references/glossary.md).

## Task References

- [ORB-12124] — shipped the first connector and its secret scanner.
- [ORB-13006] — added MP4 video uploads.
- [ORB-13027] — added the ledger v0, idempotency keys, `outcome_unknown` and media confinement.
- [ORB-13028] — added plans, the publisher, ledger v2 with threads, policy and reconcile.
- [ORB-13030] — phase 4: approvals and standing policies over the digest.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
