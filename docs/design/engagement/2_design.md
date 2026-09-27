---
title: Engagement — Design
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-27
status: Draft
feature: engagement
doc_role: design
type: design
summary: How a read is budgeted, made and recorded, how a mention is known to be answered, and how a human approves a draft.
tags: [engagement, reads, mentions, metrics, budget, ledger, approvals]
paths: ["src/pulsar/app/core/engagement/**", "src/pulsar/app/approvals.py", "src/pulsar/app/core/ledger/approvals.py", "src/pulsar/cli/commands/approve.py", "src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/ledger/reads.py", "src/pulsar/app/core/ledger/queries.py"]
related_features: [publishing, channels, surfaces]
related_artifacts: [ORB-13030]
---

# Engagement — Design

The engagement loop as built: reads through the `Reader`, and approvals of what an agent drafted. The provider side of each read is in
[Channels — Design §7](../channels/2_design.md#7-reads); the ledger rows are in the
[ledger spec](../publishing/specs/ledger.md#reads-reads).

## 1. Reads

[reader.py](../../../src/pulsar/app/core/engagement/reader.py) `Reader` makes two reads for a
bound account (identity checked, like a write):

| Read | Returns | Channel call |
|---|---|---|
| `mentions(bound, since, max_posts, caller)` | others' posts mentioning the account, and which of them it has answered | `Channel.mentions` |
| `own_posts(bound, since, max_posts, caller)` | the account's posts with their metrics | `Channel.own_posts` |

Each read runs in the publisher's order, for a call that changes nothing at the provider:

1. **Admit.** The most the read can cost, `max_posts` at the provider's `read_post_usd`, is
   checked against the day's and month's budgets (`Policy.check_read`). Quiet hours and the
   daily post cap do not apply to reads.
2. **Read** through the account's channel.
3. **Record** a `reads` row: the kind, account, window, the number of posts the provider
   returned (and billed) and their cost. From then on it counts toward the same spend as posts.
   A read that fails records nothing.

What a read returns goes back to the caller and nowhere else
([decision](./4_decisions.md#pulsar-never-stores-what-it-reads)). The account's own posts that
mention it (its side of a conversation) are dropped from `mentions`, though they were billed.

**Answered mentions.** Since ledger v3 every claimed item records the post it replies to
(`items.reply_to`, a thread's first item only). A mention is *answered* when the account has a
reply to it that went out or may have (`submitting`, `published`, `unknown`); a failed reply
answers nothing, and neither does a reply made outside pulsar.

## 2. Approvals

An agent's draft is published only under a human approval of its digest
([decision](./4_decisions.md#an-agents-draft-is-published-only-against-a-human-approval-of-its-digest)).
The row is in the [ledger spec](../publishing/specs/ledger.md#approvals-approvals).

**Recording.** `pulsar approve PLAN.yaml [--account A] [--workspace DIR] [--ttl T]`
([approve.py](../../../src/pulsar/cli/commands/approve.py) over
[app/approvals.py](../../../src/pulsar/app/approvals.py)):

1. Prepare the plan offline for each account, as `validate` does, and print to stderr every
   post in full, its media, the reply or quote target, the cost, each account's digest and any
   earlier approval of the same digest.
2. Refuse (`interactive_only`) unless stdin is a terminal. No flag skips the question.
3. Ask for the first 8 characters of each distinct digest after `sha256:`. A wrong answer
   records nothing.
4. Prepare the plan again and record one approval per account only if every digest is still
   the one shown; a plan edited in between is refused.

`approved_by` is `human:$USER`. The TTL defaults to 72 hours for a reply and 7 days
otherwise, and may be 1 minute to 30 days. `pulsar approvals` lists them with their state and
`pulsar revoke ID` revokes one. No MCP or Orbit tool records or revokes an approval.

**Using.** `Publisher.publish(..., require_approval=True)` passes `approved` to the ledger
claim, which consumes an approval in the same transaction as the policy check. An approval is
single-use: the key that used it may retry or resume, and a second key for the same content
needs a second approval. A replay of a published row needs none. `Publisher.preflight`
(a dry run) and `Publisher.approval` make the same check without consuming. A refused publish
is `approval_required`, whose message tells the human the command to run.

## 3. Plugin Tools

[app/plugin.py](../../../src/pulsar/app/plugin.py), served by
[orbit/backend.py](../../../src/pulsar/orbit/backend.py). Each is `mutating`, so an agent calls
it only from a task whose `required_tools` names it, and each records its caller as
`orbit:<task id>` (else `orbit:<agent>`) from the envelope's context.

| Tool | Input | Does |
|---|---|---|
| `pulsar.engagements` | `account`, `hours` 1–168 (24), `limit` 1–100 (20) | `Reader.mentions` over the last `hours`; returns each mention with `replied`, the cost, `complete`, and a `note` that the text is untrusted |
| `pulsar.metrics` | `account`, `days` 1–30 (7), `limit` 1–100 (20) | `Reader.own_posts`; returns each post's metrics and their `totals` (a count no post reports is null) |
| `pulsar.publish` | `source` (workspace plan file), `account`, `dry_run` | prepares every target account, then `preflight(require_approval=True)` for each, then publishes each with `require_approval=True` |

`pulsar.publish` checks every account before it sends for any, so a plan missing one approval
sends nothing. It takes no idempotency key and no caller: the default key (digest and
account) is the one the approval is used by. Its `approval_required` names the command for a
human, pinned to the plugin's home and workspace:
`PULSAR_HOME=<home> pulsar approve <workspace>/<source> --workspace <workspace> --account <alias>`.
`--workspace` makes `pulsar approve` resolve and confine media as the plugin does, so the digest
the human approves is the one the plugin computes. A dry run makes the same checks read-only
and returns the per-account reports. `pulsar.validate` on a `source` returns the same command
per account as `approve_command`, so a drafting task, which may not call `pulsar.publish`,
can hand it to the human.

## 4. Auto-Tasks

[definitions/auto_tasks/](../../../definitions/auto_tasks/), declared under
`spec.definitions.auto_tasks`. Orbit seeds them into a workspace's `.orbit/auto_tasks/` as
`pulsar-<name>`, `enabled: false`, when the plugin is enabled there; a human reviews and
switches each on. Each is `dedupe: skip_if_open`, and each minted task is tagged `pulsar`
and `no-diff-expected`, as is each publish task it creates: their commits land on the main
branch and publishing leaves no diff, so the pipeline completes them without one.

| Auto-task | Schedule (host-local) | Requires | Does |
|---|---|---|---|
| `engager` | daily 09:00 | `pulsar.engagements` | reads 24 hours of mentions (at most 20), summarises them, writes one reply plan per mention worth answering under `engagement/YYYY-MM-DD/`, validates and commits them, and creates one `proposed` task requiring `pulsar.publish` that lists each draft, its digest and its approve command |
| `post-proposer` | Fridays 16:00 | `pulsar.metrics` | reads 7 days of the account's posts, drafts up to three posts as `plan.yaml` beside their content records (no `not_before`), and creates the same kind of proposal task; drafts nothing while three already wait |
| `weekly-report` | Mondays 16:00 | `pulsar.metrics` | reports the week from `status`, `history` and one metrics read, every figure with its source and read time |

No auto-task requires `pulsar.publish` (a test holds this): drafting and publishing are separate
tasks with a human between them. Plans must be committed on the workspace's main branch,
because the plugin reads `source` from the workspace root. The workspace's own guides (voice,
strategy, content-record template) decide the details; the definitions defer to them where
they exist.

## 5. Concerns & Honest Limitations

- **The read price is an estimate.** `read_post_usd` defaults to $0.005 a post until checked on
  the X developer portal; X may also bill the author records a mentions read expands.
- **Concurrent reads can overshoot.** Two reads can both pass the budget check before either
  is recorded; the next check sees both.
- **Re-reading costs.** Overlapping windows re-read, and pay for, the same mentions, because
  pulsar keeps no inbox.
- **A read killed mid-call is unrecorded.** Pages already fetched were billed but have no row.
- **An approval is a speed bump against the same Unix user.** The terminal check and the
  typed digest stop an agent's tool call, not an agent with a shell as the same user, which
  could fake a terminal or write the ledger itself. Stronger approvals are in the
  [vision](./3_vision.md).
- **Instructions are the only guard on the drafting task's own reads.** A mention can try to
  steer the engager; the definition tells it to treat mention text as data, and nothing it
  drafts is sent without a human reading it, but a manipulated summary is still possible.
- **Media resolve against the cwd.** Without `--workspace`, `pulsar approve` resolves a plan's
  relative media against the directory it runs in and the home's `[media] roots`; the plugin
  resolves them against the workspace. The command the plugin prints passes `--workspace`.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch; approvals land here.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
