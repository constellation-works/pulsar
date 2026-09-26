"""What the ledger reports to policy: money and posts already committed.

"Committed" means a post that is in flight, published, or of unknown
outcome — anything that may have cost money. Failed and skipped posts are
free. Windows are the policy timezone's current day and calendar month.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Usage:
    spent_day_usd: float  # all accounts
    spent_month_usd: float  # all accounts
    posts_day: int  # the account being checked
