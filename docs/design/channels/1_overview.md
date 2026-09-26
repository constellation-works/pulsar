---
title: Channels — Overview
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: channels
doc_role: overview
type: design
summary: The channel adapter contract between pulsar's provider-neutral core and each social provider, and the X adapter that implements it.
tags: [channels, providers, x, adapter]
paths: ["src/pulsar/core/adapter.py", "src/pulsar/providers/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-12124, ORB-13006, ORB-13028, ORB-13031]
---

# Channels — Overview

A channel is one provider bound to one account's credentials. pulsar's core validates plans
against the channel's declared capabilities and drives it one network step at a time, so the
ledger can record each step before it happens. X is the only channel today; the contract is
shaped so a second provider (Bluesky, phase 5) is an adapter, not a rewrite.

## 1. Motivation

- **Keep the safety logic in one place.** Idempotency, policy, reconcile and the secret
  scanner are provider-independent; they must not be re-implemented per provider.
- **Providers differ in rules, not in shape.** Length units, media types and limits, threads,
  reply and quote support, and auth flows vary; "post this text with this media as this
  account" does not.
- **The ledger needs honest failure semantics.** Core can only decide "retry" versus "unknown"
  if every adapter reports failures the same way.

## 2. Core Concepts

- **Capabilities.** What a provider supports: max length and its unit, threads, reply, quote,
  delete, metrics, media types, per-type limits, media per post, alt text.
- **Offline checks.** `check_post`, `check_media`, `check_target`: provider rules applied with
  no network.
- **Network steps.** `whoami`, `upload`, `create`, `delete`, `recent_posts`, each one request
  (plus polling for video processing).
- **Fingerprint.** The channel's normalised text hash for matching its own posts on the
  provider's timeline.
- **Auth flow.** A provider's human-only login (`begin`, `complete`), separate from the
  channel because it differs most between providers.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| Contract: `Channel`, `Capabilities`, `AuthFlow` | [core/adapter.py](../../../src/pulsar/core/adapter.py) | [ORB-13028] |
| X channel, fingerprint | [providers/x/adapter.py](../../../src/pulsar/providers/x/adapter.py) | [ORB-13028] |
| X v2 API client, error mapping, chunked media | [providers/x/client.py](../../../src/pulsar/providers/x/client.py) | [ORB-12124], [ORB-13006] |
| X text rules: weighted length, URLs | [providers/x/text.py](../../../src/pulsar/providers/x/text.py) | [ORB-12124] |
| X endpoints, scopes, limits | [providers/x/config.py](../../../src/pulsar/providers/x/config.py) | [ORB-12124] |

## Task References

- [ORB-12124] — built the X client and text rules.
- [ORB-13006] — added MP4 video with chunked upload and a processing poll.
- [ORB-13028] — introduced the adapter contract and the X channel.
- [ORB-13031] — phase 5: Bluesky as the second channel.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
