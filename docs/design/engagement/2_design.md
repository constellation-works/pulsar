---
title: Engagement — Design
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Draft
feature: engagement
doc_role: design
type: design
summary: How a read is budgeted, made and recorded, how a mention is known to be answered, how a human approves a draft, and how scheduled dispatch publishes approved due plans.
tags: [engagement, reads, mentions, metrics, budget, ledger, approvals, dispatch, routines]
paths: ["src/pulsar/app/core/engagement/**", "src/pulsar/app/approvals.py", "src/pulsar/app/core/ledger/approvals.py", "src/pulsar/cli/commands/approve.py", "src/pulsar/app/core/channels/contract.py", "src/pulsar/app/core/ledger/reads.py", "src/pulsar/app/core/ledger/queries.py", "src/pulsar/app/plugin.py", ".orbit-plugin/definitions/**"]
related_features: [publishing, channels, surfaces]
related_artifacts: [ORB-13030, ORB-13375, ORB-13726, ORB-13727]
---

# Engagement — Design

The engagement loop as built: reads through the `Reader`, approvals of what an agent drafted, and the dispatcher that publishes approved plans at their slot. The provider side of each read is in
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
| `pulsar.dispatch` | `max_publish` 1–10 (3), `dry_run` | reconciles unknown writes, then publishes the approved, due plans in the configured location ([§5](#5-scheduled-dispatch)) |

`pulsar.publish` checks every account before it sends for any, so a plan missing one approval
sends nothing. It takes no idempotency key argument and no caller: the plan's `key` when it
names one, else the default key (digest and account), is the one the approval is used by. A
key the ledger already holds replays (no approval needed) or is `idempotency_conflict`
([Publishing — Design §3](../publishing/2_design.md#3-idempotency)). Its `approval_required` names the command for a
human, pinned to the plugin's home and workspace:
`PULSAR_HOME=<home> pulsar approve <workspace>/<source> --workspace <workspace> --account <alias>`.
`--workspace` makes `pulsar approve` resolve and confine media as the plugin does, so the digest
the human approves is the one the plugin computes. A dry run makes the same checks read-only
and returns the per-account reports. `pulsar.validate` on a `source` returns the same command
per account as `approve_command`, so a drafting task, which may not call `pulsar.publish`,
can hand it to the human.

## 4. Auto-Tasks

[definitions/auto_tasks/](../../../.orbit-plugin/definitions/auto_tasks/), declared under
`spec.definitions.auto_tasks`. Orbit seeds them into a workspace's `.orbit/auto_tasks/` as
`pulsar-<name>`, `enabled: false`, when the plugin is enabled there; a human reviews and
switches each on. Each is `dedupe: skip_if_open`, and each minted task is tagged `pulsar`.
Each template also carries `no-diff-expected`, so Orbit accepts an empty stage when a run
has no files to deliver. The engager can stop on an unhealthy account or find no replies
worth drafting; the post-proposer can stop on an unhealthy account or at its queue cap; x-updates
can stop on an unhealthy account or find nothing new.
The tag only exempts an empty stage: any plan files or weekly report written are still
left uncommitted for the pipeline to deliver to `agent-main`.
The post-proposer's follow-up publishing task also produces a diff when recording receipts in
content records, while the engager's and x-updates' follow-up tasks write nothing and keep
`no-diff-expected`. `auth-health` files human attention tasks and writes no files
([Accounts — Design](../accounts/2_design.md#8-offline-auth-health-alarm)).

| Auto-task | Schedule (host-local) | Requires | Does |
|---|---|---|---|
| `engager` | daily 09:00 | `pulsar.engagements`, `pulsar.status`, `pulsar.validate` | reads 24 hours of mentions (at most 20), summarises them, writes one reply plan per mention worth answering under `engagement/YYYY-MM-DD/`, validates them, leaves them uncommitted for pipeline delivery, and creates one `proposed` task requiring `pulsar.publish` that lists each draft, its digest and its approve command |
| `post-proposer` | Fridays 16:00 | `pulsar.metrics`, `pulsar.status`, `pulsar.validate` | reads 7 days of the account's posts, drafts up to three posts as `plan.yaml` beside their content records (no `not_before`), leaves them uncommitted for pipeline delivery, and creates the same kind of proposal task; drafts nothing while three already wait |
| `weekly-report` | Mondays 16:00 | `pulsar.history`, `pulsar.metrics`, `pulsar.status` | reports the week from `status`, `history` and one metrics read, every figure with its source and read time; leaves the report uncommitted for pipeline delivery |
| `x-updates` | daily 10:00 | `pulsar.history`, `pulsar.status`, `pulsar.validate` | scans constellation-works with `gh` (read-only, 7 days) for releases, newly public repos and notable merged PRs; keys each (`release:<repo>:<tag>`, `repo:<name>`, `pr:<repo>:<n>`) with the skill's `x_updates.py`, skips keys `pulsar.history` `keys` finds in the ledger (imported history included) or already drafted (a plan file under `x-updates/`, an open `pulsar-x-update-posts` task); writes at most three keyed plans under `x-updates/YYYY-MM-DD/`, validates them, leaves them uncommitted, and files one proposal task; nothing new writes nothing ([Publishing — Design §8](../publishing/2_design.md#8-importing-postedjsonl-and-the-x-updates-auto-task)) |

No auto-task requires `pulsar.publish` (a test holds this): drafting and publishing are separate
tasks with a human between them. Plans are delivered to the workspace's main branch by the pipeline,
because the plugin reads `source` from the workspace root. The workspace's own guides (voice,
strategy, content-record template) decide the details; the definitions defer to them where
they exist.

## 5. Scheduled Dispatch

An Orbit task runs when it is promoted, not at a plan's `not_before`. `pulsar.dispatch`
([app/plugin.py](../../../src/pulsar/app/plugin.py) `dispatch`) publishes a plan at its slot
without a model or a promotion, and only what a human already approved
([decision](./4_decisions.md#scheduled-dispatch-publishes-approved-due-plans-and-ships-disabled)).

**Where plans come from.** `[dispatch] plans` in the home's `config.toml`: glob patterns
relative to the workspace, for example `["x-updates/**/*.yaml", "engagement/**/*.yaml"]`
([config reference](../publishing/references/config.md)). A pattern that is absolute, starts
with `~` or has a `..` component is `invalid_config`, and the tool takes no path, so the
caller cannot point it elsewhere. A match under a dot-directory (`.git`, `.orbit` and its
worktrees) is ignored, and each file is read through `read_source`, so a symlink out of the
workspace is an error, not a plan. At most 200 files are looked at per call, in path order
(`truncated` says when there were more). No patterns, no plans: the call only reconciles.

**One call**, in order:

1. **Reconcile.** For each account with an `unknown` or abandoned `submitting` row, what
   `pulsar reconcile --account A` does (`reconciled[]`: the pending keys and the verdicts).
2. **Publish.** For each plan file, every target account must pass, else the plan is
   `skipped[]` with a `reason` (and `retry_after` where the refusal names one):

   | Reason | When | Later call |
   |---|---|---|
   | `unscheduled` | the plan has no `not_before`; its publish task publishes it | never |
   | `not_due` | `not_before` is later | at the slot |
   | `published` | every account's key is published or skipped in the ledger | never |
   | `in_flight` | a row is `submitting` or `unknown`, or another caller claimed it first | after reconcile |
   | `failed` | a row is `failed` or `partial`; a human retries with `pulsar.publish` or `pulsar publish` | no |
   | `approval_required` | no live approval of the current digest; the message has the approve command | after approval |
   | `approval_stale` | an active approval recorded for this file's path is for another digest: it was edited after approval | after a new approval |
   | `quiet_hours`, `budget_exceeded`, `daily_cap` | the policy refuses now | when the window moves |
   | `tick_limit`, `deadline` | `max_publish` plans were published, or the call ran 40 s | next call |

   The checks are `Publisher.preflight(require_approval=True)`, as for `pulsar.publish`.
   A plan that passes is published to each account with
   `Publisher.publish(require_approval=True)` under the plan's `key` (else the default key),
   so the approval is consumed in the claim transaction. `published[]` carries each receipt
   with its `source`. A plan that cannot be read or prepared is in `errors[]`.

Dispatch never records, revokes or skips an approval; it publishes under exactly the rule
`pulsar.publish` does, and a plan without `not_before` is never its to send.

**Racing.** Two dispatch calls, or a call and a manual `pulsar publish`/`pulsar.publish`, use
the same key for the same plan, so the ledger decides: the claim and `begin_item`'s
compare-and-set let one caller send each post, and the other sees `outcome_unknown`
(`in_flight`) or, once it is done, a replay. Tests race four processes and a manual publish on
the fake transport and count one provider call per plan.

**Bounds.** `max_publish` (default 3) plans per call; no reconcile or publish starts after
40 s, and uploads stop at the publish cap (55 s), inside the backend's 60 s timeout.
`dry_run` lists the pending keys and the plans a live call would publish (`ready[]`), and
calls, records and consumes nothing.

**The routine.** The plugin ships three definitions
([definitions/](../../../.orbit-plugin/definitions/)): the activity `pulsar_dispatch`
(`type: deterministic`, `action: plugin.tool_call`, `tool: pulsar.dispatch`,
`max_publish: 3`, no prompt or model), the job `pulsar_dispatch_pipeline` (one step,
`max_active_runs: 1`), and the routine `dispatch` (cron `*/5 * * * *`, `missed_run: skip`,
`overlap: forbid`, `timeout_minutes: 5`). Orbit seeds the routine into the workspace's
`.orbit/routines/` as `pulsar-dispatch`, `enabled: false`; the activity and job are a catalog
layer. A human sets `[dispatch] plans` and enables it. Its writes are recorded with the caller
the backend derives from the step's context (`orbit:<agent>`, else `orbit`; a routine has no task id).

## 6. Concerns & Honest Limitations

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

- **Dispatch reconciles unattended.** A reconcile reads the account's timeline, billed per
  post returned, on every call while an unknown row stays unsettled; it is not checked
  against the budget first.
- **Dispatch publishes whenever the slot has come.** A plan approved long after its
  `not_before` goes out at the next tick, not at the original time; quiet hours are the only
  window it keeps.
- **A glob over a large tree is slow.** Patterns should start with the plan directory
  (`x-updates/**`), not `**`, which walks every file under the workspace before filtering.

## Task References

- [ORB-13030] — phase 4: drafts, approvals, standing policies, dispatch; approvals land here.
- [ORB-13375] — allow auto-task delivery on documented no-file paths while preserving file delivery.
- [ORB-13727] — scheduled dispatch: `pulsar.dispatch` and the `pulsar-dispatch` routine.
- [ORB-13726] — the x-updates auto-task; `pulsar.publish` honours a plan's `key`.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
