---
type: design
summary: "Spec: publishing policy — quiet hours, daily cap, day and month budgets, retry_after"
last_validated: 2026-09-26
---

# Spec: Publishing Policy

Before any network call, a plan is admitted only if it fits the policy against what the ledger
has committed or reserved. The check and the ledger claim are one transaction, so two plans
cannot both be admitted with the same headroom. The policy applies to every surface that
posts: `create_post`, `pulsar publish`, and the plugin's publishing tools when they exist.

## Why This Exists

An agent loop or a misfiring routine can post until the account's budget is gone. Limits
checked after the fact only report the damage.

## Rules

Checked in order; the first failure is reported:

1. **`quiet_hours`** — now is inside `[start, end)` on the wall clock of `policy.timezone`;
   the window may wrap midnight (`23:00-07:00`).
2. **`daily_cap`** — the account's posts today plus the plan's posts exceed
   `max_posts_per_day`. Every post of a thread counts.
3. **`budget_exceeded`** — spend today (all accounts) plus the plan's estimated cost exceeds
   `daily_budget_usd`; then the same for the calendar month and `monthly_budget_usd`
   (`detail.window` is `day` or `month`).

Usage is defined in [ledger.md](./ledger.md#usage-for-policy).

## Arithmetic

- Limits are inclusive: a plan that lands exactly on a cap or budget passes.
- Money is compared in exact decimals (6 places).
- A budget of 0 stops every paid plan; a cap of 0 stops every post. A plan that costs nothing
  is never stopped by a budget, only by the cap.
- A provider with no `[prices.<provider>]` table is priced at zero.

## `retry_after`

All three codes carry `detail.retry_after` (ISO 8601 UTC) and `retryable: true`: the end of
the quiet window, the next local midnight, or the first instant of the next local month.

A plan too big to ever pass on its own — more posts than the cap, or costing more than a
budget — is refused with `retryable: false` and `retry_after: null`.

## Time

- Days and months are local to `policy.timezone` (IANA, default `UTC`), so a day is 23 or 25
  hours across a DST change.
- A quiet-hours end skipped by a spring-forward gap ends the window at the jump.
- On a fall-back night a repeated wall-clock hour inside the window is quiet both times.

## Defaults

$1/day and $10/month across accounts, 5 posts per account per day, no quiet hours, UTC day
boundary (agreed with Daniel for the posting host, and the code's defaults). Keys: [../references/config.md](../references/config.md).
