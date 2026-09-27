---
title: Channels — Decisions
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: channels
doc_role: decisions
type: design
summary: Why the fingerprint ignores links and leading mentions, why only X's copy is unescaped, and why prices live in config.
tags: [channels, x, fingerprint, pricing]
paths: ["src/pulsar/app/core/channels/x/adapter.py", "src/pulsar/app/settings.py"]
related_features: [publishing]
related_artifacts: [ORB-13027, ORB-13028]
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

## Task References

- [ORB-13027] — moved prices out of constants into config.
- [ORB-13028] — added the fingerprint and reconcile.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
