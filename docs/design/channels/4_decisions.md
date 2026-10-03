---
title: Channels — Decisions
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Accepted
feature: channels
doc_role: decisions
type: design
summary: Why the fingerprint ignores links and leading mentions, why only X's copy is unescaped, why prices live in config, and how Bluesky media and reconcile fit the contract.
tags: [channels, x, bluesky, fingerprint, pricing, reconcile]
paths: ["src/pulsar/app/core/channels/x/adapter.py", "src/pulsar/app/core/channels/bluesky/adapter.py", "src/pulsar/app/settings.py"]
related_features: [publishing]
related_artifacts: [ORB-13027, ORB-13028, ORB-13031]
---

# Channels — Decisions

Non-obvious decisions about providers. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## Fingerprint what survives the provider's rewriting

**Recorded:** 2026-09-26 · [ORB-13028]
**Code anchors:** `src/pulsar/app/core/channels/x/adapter.py::fingerprint`, `src/pulsar/app/core/channels/x/adapter.py::remote_fingerprint`

### Context

Reconcile must find a post on X's timeline by its text. X shortens every link to `t.co`,
appends a media link, prepends `@handles` to replies and HTML-escapes `&<>`, so an exact text
hash never matches.

### Decision

The fingerprint drops every URL and any leading run of `@handles`, applies NFC and collapses
whitespace. HTML entities are unescaped on X's copy only (`remote_fingerprint`), because a
local `&amp;` is literal text the author wrote.

### Consequences

- A post pulsar sent is found again despite X's rewriting.
- Cost: posts that differ only in links or leading mentions fingerprint alike; reconcile can
  mis-attribute which of two such posts went out (never towards a second post).

## Prices are configuration, not code

**Recorded:** 2026-09-26 · [ORB-13027]
**Code anchors:** `src/pulsar/app/settings.py::Settings.prices_for`

### Context

Budgets are enforced from estimated cost. X changed its API pricing more than once, and a
new provider has its own table.

### Decision

Anything a provider can change without a pulsar release (prices, and by extension limits that
are account entitlements) lives in `config.toml`, with defaults in code. A provider with no
table is priced at zero.

### Consequences

- A price change is a config edit on the posting host, not a release.
- Cost: a stale table silently mis-states cost in both directions; validate says
  "verify on the provider's portal" because nothing checks it.

## A Bluesky media id is the blob's CID, kept by the channel

**Recorded:** 2026-10-03 · [ORB-13031]
**Code anchors:** `src/pulsar/app/core/channels/bluesky/adapter.py::BlueskyChannel.upload`, `src/pulsar/app/core/channels/bluesky/adapter.py::BlueskyChannel.create`

### Context

The contract's `upload` returns one string and `create` takes those strings back. X keeps an
uploaded media object by id and takes alt text separately; Bluesky has no media object: a post
embeds the whole blob (CID, MIME type, size) with its alt text inline.

### Decision

`upload` returns the blob's CID and the channel keeps the blob and its alt text for `create`.
A media id the channel did not upload is `invalid_media` before anything is sent.

### Consequences

- The contract is unchanged, and the ledger records a provider's real id.
- Cost: the ids are only good on the channel that uploaded them. The publisher uploads each
  item's media just before creating it, so a publish never crosses channels; a resumed publish
  uploads again, which Bluesky deduplicates by content.

## Bluesky reconcile lists the account's records

**Recorded:** 2026-10-03 · [ORB-13031]
**Code anchors:** `src/pulsar/app/core/channels/bluesky/adapter.py::BlueskyChannel.recent_posts`

### Context

An AT Protocol record can be created with a caller-chosen key, which would let reconcile ask
for one record by name. `Channel.create` gets no idempotency key or item index to derive a key
from.

### Decision

`recent_posts` lists the account's own post records back to the claim's window, like X's
timeline read, and reconcile matches them by fingerprint. Listing is free on Bluesky.

### Consequences

- Reconcile is the same code for both providers; the contract needs no new argument.
- Cost: matching is by fingerprint, with the collisions X has (fewer: Bluesky does not
  rewrite links), and is bounded to five pages of 100 records.

## Task References

- [ORB-13027] — moved prices out of constants into config.
- [ORB-13028] — added the fingerprint and reconcile.
- [ORB-13031] — added the Bluesky channel: blob-CID media ids and record-listing reconcile.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
