---
title: Channels — Vision
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Draft
feature: channels
doc_role: vision
type: design
summary: Per-provider auth flows (Bluesky's login next), metrics, and how variants are chosen.
tags: [channels, providers, bluesky, mastodon, linkedin, metrics]
paths: ["src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/channels/**"]
related_features: [accounts, publishing]
related_artifacts: [ORB-13031]
---

# Channels — Vision

What the adapter boundary is expected to carry next. Nothing here is built; the Bluesky
channel itself is, in [2_design.md](./2_design.md).

## 1. Open Questions

1. **The contract survived Bluesky.** [ORB-13031] added it without changing `Channel`:
   facets, reply references and media embeds all moved into `create`, and media ids became
   blob CIDs the channel keeps until then. What is left open is whether a provider's media
   should outlive one channel instance (a resumed publish uploads again).
2. **What does `AuthFlow` need?** Bluesky's login (OAuth with PAR, PKCE and DPoP) is the
   next task: it must store the DPoP key, PDS URL and token endpoint with the bundle and wire
   `Es256Proof` into the factory. Mastodon needs
   per-instance dynamic client registration; LinkedIn needs a confidential client secret, which
   pulsar would have to hold like a token. The protocol's `begin`/`complete` may not be enough.
3. **Should channels report metrics?** Marketing's results-review auto-task wants impressions
   and engagement. `Capabilities.metrics` exists but is false everywhere; reads were declared out
   of pulsar's scope, so this needs a decision, not just code.
4. **How are variants chosen?** A plan's `variants: {bsky: {...}}` replaces posts per provider;
   whether pulsar should ever derive a variant (shorten, split) or only validate the author's is
   open. The current answer is only validate.

## 2. Prior Work

### Multi-network publishers

Buffer, Hootsuite and similar tools normalise posts across networks and expose per-network
overrides; the overrides are the part that never goes away.

### Protocol docs

AT Protocol (Bluesky) lexicons for `app.bsky.feed.post` and rich-text facets; Mastodon's
statuses API; X API v2.

## 3. What May Be Distinctive

Failure semantics are part of the contract: every adapter must separate "did not act" from
"may have acted", which is what makes one provider-neutral ledger possible.

## 4. References

**pulsar-internal**

- [Channels — Design](./2_design.md)
- [Accounts — Vision](../accounts/3_vision.md)

**External**

- AT Protocol and Bluesky API docs; Mastodon API docs; X API v2 docs.

## Task References

- [ORB-13031] — phase 5: Bluesky through the adapter boundary; left the login to a sibling task.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
