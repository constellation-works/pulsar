"""Paid reads: one row per call, recorded after it returns.

A read changes nothing at the provider, so it is recorded once it has
happened, with what it cost; never what it returned.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from . import text
from .records import iso

type ReadKind = Literal["mentions", "own_posts"]


def record_read(
    conn: sqlite3.Connection,
    now: str,
    *,
    kind: ReadKind,
    provider: str,
    account_alias: str,
    caller: str | None,
    since: datetime,
    posts: int,
    est_cost_usd: float,
    complete: bool,
) -> int:
    cur = conn.execute(
        "INSERT INTO reads (kind, provider, account_alias, caller, since, posts,"
        " est_cost_usd, complete, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, provider, account_alias, text.persisted_text(caller), iso(since), posts,
         float(est_cost_usd), int(complete), now),
    )  # fmt: skip
    return int(cur.lastrowid or 0)
