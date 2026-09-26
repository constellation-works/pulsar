---
title: Publishing — Vision
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Draft
feature: publishing
doc_role: vision
type: design
summary: Drafts, approvals bound to the digest, standing policies for routines, and a dispatch routine with no model in the publish path.
tags: [publishing, approvals, drafts, dispatch, routines]
paths: ["src/pulsar/core/publisher.py", "src/pulsar/core/ledger/**"]
related_features: [surfaces, channels]
related_artifacts: [ORB-13030, ORB-13115]
---

# Publishing — Vision

Phase 4 ([ORB-13030]) puts a human approval between an agent's draft and a paid post, and a
standing policy in its place for routines Daniel enabled. Everything here is planned, not
built.

## 1. Open Questions

1. **Where does content live?** Marketing's content records are `post.md` files. The
   recommendation is a fenced ` ```pulsar ` YAML block inside `post.md` (one source of truth);
   the alternative is a sibling `post.yaml`. Undecided.
2. **How long does an approval last?** Approval records `{digest, approver, ts, ttl}`; the
   default TTL is undecided.
3. **How does a standing policy know its caller?** A policy such as `{routine:
   constworks-x-updates, max_per_day: 1}` must match a host-attested identity, not a
   self-asserted `caller`. Orbit's plugin context carries no task or run id yet ([ORB-13115]).
4. **Should dispatch reconcile?** A deterministic dispatch routine publishing due, approved
   drafts is also the natural place to reconcile unknown rows; whether it may spend on
   timeline reads unattended is open.

## 2. Prior Work

### Transactional outbox

Record the intent in the same store as the decision, then deliver from it. pulsar's ledger is an
outbox whose delivery is a paid, non-idempotent API.

### Idempotency keys in payment APIs

Stripe-style keys: the client names the operation, the server stores the result and replays
it. pulsar adds `unknown`, because it sits on the client side of an API that has no keys.

### Social scheduling tools

Buffer, Typefully and similar queue posts and gate them on a human click. pulsar's approval
binds to a content digest instead of a queue entry, so an edit after approval is caught.

## 3. What May Be Distinctive

- Approval bound to the digest of the content, re-checked at publish time by re-reading and
  re-digesting the source (drift is `approval_stale`).
- `unknown` as a first-class state with a reconcile that prefers stuck over double-posted.
- No model in the publish path: dispatch is a deterministic tool call.

## 4. References

**pulsar-internal**

- [Publishing — Design](./2_design.md)
- [Surfaces — Vision](../surfaces/3_vision.md) for the plugin tools that carry this

**External**

- Transactional outbox pattern; Stripe API idempotent requests.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch, the x-updates port.
- [ORB-13115] — Orbit: host-attested task and run id in the plugin context (ws_orbit).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
