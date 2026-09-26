---
type: design
summary: "Glossary: publishing"
last_validated: 2026-09-26
---

# Glossary: Publishing

pulsar's publishing vocabulary. General terms (WAL, idempotency, OAuth) are left out unless
pulsar gives them a narrower meaning.

| Term | Meaning |
|------|---------|
| **Absent** | Reconcile's verdict that an ambiguous post did not reach the provider (`outcome_resolved_absent`); the key re-sends it. [specs/ledger.md](../specs/ledger.md) |
| **Digest** | `sha256:` over a plan's canonical content (accounts, text, media hashes and alt, reply/quote, variants); binds approval and the default key. [2_design.md §1](../2_design.md) |
| **Fingerprint** | A channel's normalised text hash used to find a post on the provider's timeline. [Channels — Design](../../channels/2_design.md) |
| **Idempotency key** | The ledger row's unique key; a repeat with it never makes a second post. [specs/idempotency.md](../specs/idempotency.md) |
| **Item** | One post of a plan row's thread, with its own state and cost. [specs/ledger.md](../specs/ledger.md) |
| **Partial** | A plan row with some posts published and the rest provably not; a repeat resumes it. [specs/ledger.md](../specs/ledger.md) |
| **Plan** | Provider-neutral description of what to publish. [2_design.md §1](../2_design.md) |
| **Re-arm** | Retrying a `failed` or `partial` row: failed items get the current text hash, fingerprint and price. [specs/ledger.md](../specs/ledger.md) |
| **Receipt** | What a published row returns on a repeat, with `replayed: true`. [specs/idempotency.md](../specs/idempotency.md) |
| **Reconcile** | Settling unknown and stale posts against the account's timeline. [2_design.md §6](../2_design.md) |
| **Reservation** | The unsent posts of a row still being published, counted against budget and cap. [specs/ledger.md](../specs/ledger.md#usage-for-policy) |
| **Skipped** | A key recorded as never to be published (imported from `posted.jsonl`). [2_design.md §8](../2_design.md) |
| **Stale** | A `submitting` post older than the ten-minute window: its sender is presumed dead. [2_design.md §6](../2_design.md) |
| **Unknown** | A post that may have reached the provider; blocks its key until reconciled. [specs/idempotency.md](../specs/idempotency.md) |
| **Usage** | What the ledger has committed or reserved today and this month, per account and overall. [specs/ledger.md](../specs/ledger.md#usage-for-policy) |
