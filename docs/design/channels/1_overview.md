---
title: Channels — Overview
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Accepted
feature: channels
doc_role: overview
type: design
summary: The channel adapter contract between pulsar's provider-neutral core and each social provider, and the X and Bluesky adapters that implement it.
tags: [channels, providers, x, bluesky, adapter]
paths: ["src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/channels/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-12124, ORB-13006, ORB-13028, ORB-13031, ORB-13729]
---

# Channels — Overview

A channel is one provider bound to one account's credentials. pulsar's core validates plans
against the channel's declared capabilities and drives it one network step at a time, so the
ledger can record each step before it happens. X and Bluesky are the channels today; Bluesky
was added as an adapter behind the same contract, with no change to it.

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
- **Auth flow.** A provider's human-only login (`authorize`, which stores nothing), separate
  from the channel because it differs most between providers.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| Contract: `Channel`, `Capabilities`, `AuthFlow` | [app/core/channels/contract.py](../../../src/pulsar/app/core/channels/contract.py) | [ORB-13028], [ORB-13729] |
| X channel, fingerprint | [app/core/channels/x/adapter.py](../../../src/pulsar/app/core/channels/x/adapter.py) | [ORB-13028] |
| X v2 API client, error mapping, chunked media | [app/core/channels/x/client.py](../../../src/pulsar/app/core/channels/x/client.py) | [ORB-12124], [ORB-13006] |
| X text rules: weighted length, URLs | [app/core/channels/x/text.py](../../../src/pulsar/app/core/channels/x/text.py) | [ORB-12124] |
| X endpoints, scopes, limits | [app/core/channels/x/config.py](../../../src/pulsar/app/core/channels/x/config.py) | [ORB-12124] |
| Bluesky channel: records, facets, embeds, reads | [app/core/channels/bluesky/adapter.py](../../../src/pulsar/app/core/channels/bluesky/adapter.py) | [ORB-13031] |
| Bluesky XRPC client, refresh, DPoP hook, error mapping | [app/core/channels/bluesky/client.py](../../../src/pulsar/app/core/channels/bluesky/client.py) | [ORB-13031] |
| Bluesky login: atproto OAuth (PAR, PKCE, DPoP) | [app/core/channels/bluesky/auth.py](../../../src/pulsar/app/core/channels/bluesky/auth.py) | [ORB-13729] |
| Bluesky text rules: graphemes, facets | [app/core/channels/bluesky/text.py](../../../src/pulsar/app/core/channels/bluesky/text.py) | [ORB-13031] |
| Provider -> channel factory | [app/runtime.py](../../../src/pulsar/app/runtime.py) `client_for`, `channel`, `login_flow` | [ORB-13031], [ORB-13729] |

## Task References

- [ORB-12124] — built the X client and text rules.
- [ORB-13006] — added MP4 video with chunked upload and a processing poll.
- [ORB-13028] — introduced the adapter contract and the X channel.
- [ORB-13031] — phase 5: Bluesky as the second channel, and the provider -> channel factory.
- [ORB-13729] — Bluesky's login, the first `AuthFlow`.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
