---
title: Engagement — Decisions
owner: claude
last_updated: 2026-10-03
last_validated: 2026-09-27
status: Draft
feature: engagement
doc_role: decisions
type: design
summary: An agent's draft needs a human approval of its digest; reads share the write budget; pulsar never stores what it reads.
tags: [engagement, approvals, reads, budget]
paths: ["src/pulsar/app/core/ledger/**", "src/pulsar/app/plugin.py"]
related_features: [publishing, surfaces]
related_artifacts: [ORB-13030, ORB-13115, ORB-13727, ORB-13728]
---

# Engagement — Decisions

Standing rules for reads and the engagement loop. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## An agent's draft is published only against a human approval of its digest

**Recorded:** 2026-09-27 · [ORB-13030]

### Context

The engagement auto-tasks draft replies and posts, and a later task publishes them. Between
the two, a human decides. Orbit records who promoted the task, but its guidance is that
attribution and provenance grant no authority, and nothing stops an agent from editing a draft
after the human read it.

### Decision

A publish on behalf of an agent-drafted plan needs an approval recorded in the ledger for that
account and that plan's digest, unexpired and unrevoked. The check runs inside the
transaction that claims the write, so no path publishes a new claim without it. Approvals are
recorded only by a terminal command; no agent tool, on any surface, can record one. An edit
changes the digest and needs a new approval; a reschedule does not (the slot is outside the
digest). New publish paths for agent-drafted content follow the same rule.

### Consequences

- The human approves bytes, not a task title: what they read is what goes out.
- Cost: every draft needs a command run by a human before the task that publishes it is
  promoted, and an approval that expires unused means drafting or approving again.

## Standing policies are declined until caller identity can be attested

**Recorded:** 2026-10-03 · [ORB-13728]

### Context

A standing policy such as automatically thanking first-time mentioners would let an agent
publish without a per-plan human approval. Agent-drafted plans always require that approval,
and safely matching a standing policy to a caller needs host-attested identity from Orbit
([ORB-13115]).

### Decision

Decline standing policies for now. Every agent-drafted plan continues to require a human
approval. Revisit only after [ORB-13115] lands and Daniel reopens the decision.

### Consequences

- No agent can publish autonomously under a standing rule, even for a narrow class of replies.
- Cost: repetitive, low-risk replies still wait for a human to approve each plan.

## Scheduled dispatch publishes approved due plans and ships disabled

**Recorded:** 2026-10-03 · [ORB-13728]

### Context

Orbit tasks run when promoted, while a plan's `not_before` slot may be later. Publishing at
that slot should not require a human to promote the task at the right time, and unknown write
rows still need reconciliation.

### Decision

Build a deterministic routine with no model that reconciles unknown rows and publishes plans
only when they already have a human approval and are due (`not_before`). The routine ships
seeded disabled; a human must enable it. Implementation is tracked by [ORB-13727].

### Consequences

- Dispatch cannot invent or approve content; each plan still needs its own human approval.
- Cost: the routine does nothing until a human enables it, and it only dispatches work already
  approved and due.

## Budget, quiet hours, and read-price defaults remain as configured

**Recorded:** 2026-10-03 · [ORB-13728]

### Context

The box currently uses a $1 daily budget, a $10 monthly budget, and a cap of five posts per
day, with no quiet window configured. The configured `read_post_usd` value is still an
estimate that has not been checked against the X developer portal.

### Decision

Keep the $1/day and $10/month budgets and the five-post daily cap; leave quiet hours
unconfigured. Keep `read_post_usd` at the $0.005 estimate until a human checks the price in
the X developer portal.

### Consequences

- The existing spend and post limits remain in force; no time-of-day restriction is added.
- Cost: read charges may differ from the unverified estimate, and dispatch may run at any hour.

## Reads spend from the same budget as writes

**Recorded:** 2026-09-27

### Context

X bills reads per post returned. A loop that reads mentions every day and metrics every week
spends money unattended, and a separate read budget would be one more number to set and one
more way to overspend.

### Decision

A read's estimated cost is checked against the daily and monthly budgets before the call and
recorded in the ledger after it, and it counts toward the same spend as posts. Reads do not
count toward the daily post cap, quiet hours do not apply to them, and the plugin's read tools
are `mutating`, so only a task that names them in `required_tools` can call them.

### Consequences

- One budget answers "how much can pulsar spend today".
- Cost: a heavy read day can leave no budget for a post, and a read that returns more posts
  than estimated is recorded after the fact, so the budget can overshoot by one call.

## pulsar never stores what it reads

**Recorded:** 2026-09-27

### Context

Mentions carry other people's text and handles. The ledger keeps hashes and ids so it can
stay on the host indefinitely without becoming a copy of anyone's content.

### Decision

Read results go to the caller and nowhere else. The ledger records that a read happened (kind,
account, window, count, cost), never the text or authors it returned. Whether a mention has
been answered is derived from pulsar's own replies (the post id each reply answers), not from
a stored inbox.

### Consequences

- The ledger stays free of third-party content; what an agent keeps is the workspace's
  business and its review.
- Cost: overlapping windows re-read, and pay for, the same mentions, and a reply made outside
  pulsar does not mark its mention answered.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch; approvals land here.
- [ORB-13115] — Orbit: add host-attested task and run identity to plugin context.
- [ORB-13727] — implement scheduled dispatch for approved due plans, seeded disabled.
- [ORB-13728] — record the 2026-10-03 engagement decisions.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
