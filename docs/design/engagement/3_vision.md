---
title: Engagement — Vision
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Draft
feature: engagement
doc_role: vision
type: design
summary: Direct messages, scheduled publishing of approved drafts, host-attested approvals, and what else the engagement loop may grow into.
tags: [engagement, dms, dispatch, approvals]
paths: ["src/pulsar/app/plugin.py", "definitions/auto_tasks/*.yaml"]
related_features: [publishing, surfaces]
related_artifacts: [ORB-13030, ORB-13115]
---

# Engagement — Vision

What the engagement loop may grow into. Nothing here is built.

## 1. Open Questions

1. **Direct messages.** DMs need the `dm.read` and `dm.write` scopes, so every bound account
   re-authorizes, and they carry private text pulsar would hand to an agent. Deferred until
   mentions and replies have run for a while.
2. **Scheduled publishing.** A post's slot (`not_before`) is outside the digest, so an
   approval survives a reschedule, but an Orbit task runs when it is promoted, not at the slot.
   Today the human's promotion is the timing. A deterministic dispatch routine (no model)
   publishing due, approved plans would free the human from promoting on the day.
3. **Stronger approvals.** An approval is recorded by a terminal command run as the same Unix
   user an agent's shell runs as. Orbit could attest the approver (a dashboard click, a
   host-signed record), or approvals could need a passphrase an agent never sees.
4. **Replies without a proposal task.** A standing policy (for example, "thank-you replies to
   first-time mentioners, at most three a day") would let the engager publish without a
   human in the loop. It needs a host-attested caller identity ([ORB-13115]).
5. **The read price.** X bills reads per post returned; the default price in `config.toml` is
   an estimate until checked against the developer portal.

## 2. Prior Work

### Social inbox tools

Hootsuite, Sprout Social and similar tools collect mentions into an inbox and let a person
reply. pulsar's inbox is a task: the agent does the triage and drafting, the human only
approves.

### Two-person rule

A draft written by one actor and approved by another, with the approval bound to the exact
content, is the two-person rule applied to publishing.

## 3. What May Be Distinctive

- Approval bound to a digest and checked in the ledger's claim transaction, so a replay, a
  retry or a resumed thread is judged the same way as the first attempt.
- The engagement loop is ordinary Orbit tasks and auto-tasks: no scheduler, queue or inbox of
  pulsar's own.

## 4. References

**pulsar-internal**

- [Engagement — Overview](./1_overview.md)
- [Publishing — Vision](../publishing/3_vision.md) for dispatch and standing policies

**External**

- X API v2: mentions timeline, post lookup with public and non-public metrics.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch.
- [ORB-13115] — Orbit: host-attested task and run id in the plugin context (ws_orbit).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
