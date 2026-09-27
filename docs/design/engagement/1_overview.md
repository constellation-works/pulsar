---
title: Engagement — Overview
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Draft
feature: engagement
doc_role: overview
type: design
summary: Reads for engagement (mentions, post metrics) and the plugin's auto-tasks that turn them into drafts a human approves before anything is published.
tags: [engagement, reads, mentions, metrics, approvals, auto-tasks, orbit-plugin]
paths: ["src/pulsar/app/core/channels/**", "src/pulsar/app/core/ledger/**", "src/pulsar/app/plugin.py", "definitions/auto_tasks/*.yaml"]
related_features: [publishing, channels, surfaces]
related_artifacts: [ORB-13030]
---

# Engagement — Overview

pulsar reads what an account needs to answer for itself (mentions of it and how its own posts
performed) and hands that to agents running in the one workspace that has the plugin on.
Three auto-tasks shipped with the plugin turn the reads into work: an engager that drafts
replies, a proposer that drafts new posts from what performed, and a weekly report. Nothing
they draft is published until a human has approved the exact content: the approval is bound
to the content's digest, recorded in the ledger, and checked inside the same transaction that
claims the write.

## 1. Motivation

- **Marketing had no read source.** Its weekly results review recorded X metrics as
  unavailable, and nobody saw replies to the account unless they opened X.
- **A task promotion is not an approval.** Orbit records who promoted a task, but attribution
  grants no authority, and an agent can edit a draft after the human read it. The publish path
  needs its own proof that a human approved exactly these bytes.
- **Reads cost money too.** X bills reads per post returned, so reads go through the same
  budget as writes and are recorded in the same ledger.

## 2. Core Concepts

- **Read.** A paid call that returns posts: the account's mentions in a window, or its own
  recent posts with their metrics. Recorded in the ledger (kind, count, estimated cost),
  never the text.
- **Approval.** A human's yes to one plan for one account, keyed by the plan's digest, with an
  expiry. Recorded in the ledger by a terminal command no agent tool can reach.
- **Proposal task.** An Orbit task, created `proposed`, that lists drafts (plan files in the
  workspace), their digests and the exact approve commands, and names `pulsar.publish` in its
  `required_tools`. A human approves the drafts, then promotes the task; the agent that runs it
  publishes.
- **Engagement loop.** Read → draft → propose → human approves and promotes → publish →
  report. The auto-tasks are the read, draft and report steps; the publish step is the
  proposal task.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| Read calls on the provider contract | [channels/contract.py](../../../src/pulsar/app/core/channels/contract.py) | — |
| The approvals and reads tables | [ledger/schema.py](../../../src/pulsar/app/core/ledger/schema.py) | [ORB-13030] |
| The plugin's engagement tools | [app/plugin.py](../../../src/pulsar/app/plugin.py) | — |
| The shipped auto-tasks | [definitions/auto_tasks/](../../../definitions/auto_tasks/) | — |
| The human approval verbs | [cli/commands/approve.py](../../../src/pulsar/cli/commands/approve.py) | — |

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch; approvals land here.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
