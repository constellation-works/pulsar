"""Reads over an open connection: loading rows with their items, and usage."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime

from pulsar.internal.errors import INTERNAL, INVALID_ARGUMENT, PulsarError

from .records import (
    COMMITTED_ITEM_STATES,
    OPEN_ROW_STATES,
    PENDING,
    PUBLISHED,
    SUBMITTING,
    UNKNOWN,
    ItemRecord,
    PlanRecord,
    WriteRecord,
    iso,
)
from .usage import Usage


def with_items(conn: sqlite3.Connection, row: sqlite3.Row) -> PlanRecord:
    items = conn.execute("SELECT * FROM items WHERE write_id = ? ORDER BY idx", (row["id"],))
    return PlanRecord.from_rows(row, items.fetchall())


def get_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM writes WHERE idempotency_key = ?", (key,)
    ).fetchone()
    return row


def missing_row(key: str) -> PulsarError:
    """Every transition after the claim names a key the claim wrote; a missing
    row is a broken invariant (a caller bug, or the file was replaced)."""
    return PulsarError(
        INTERNAL,
        f"the ledger has no row for idempotency_key {key!r}; claim it before settling it",
        detail={"idempotency_key": key},
    )


def require_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = get_row(conn, key)
    if row is None:
        raise missing_row(key)
    return row


def get_write(conn: sqlite3.Connection, key: str) -> WriteRecord | None:
    row = get_row(conn, key)
    return None if row is None else WriteRecord.from_row(row)


def load(conn: sqlite3.Connection, key: str) -> PlanRecord | None:
    row = get_row(conn, key)
    return None if row is None else with_items(conn, row)


def require(conn: sqlite3.Connection, key: str) -> PlanRecord:
    return with_items(conn, require_row(conn, key))


def write_id(conn: sqlite3.Connection, key: str) -> int:
    return int(require_row(conn, key)["id"])


def item(record: PlanRecord, idx: int) -> ItemRecord:
    for candidate in record.items:
        if candidate.idx == idx:
            return candidate
    raise PulsarError(
        INVALID_ARGUMENT,
        f"idempotency_key {record.key!r} has no post {idx} (it has {len(record.items)})",
        detail={"idempotency_key": record.key, "idx": idx},
    )


def known_post_ids(conn: sqlite3.Connection, wanted: Sequence[str]) -> set[str]:
    marks = ", ".join("?" for _ in wanted)
    rows = conn.execute(
        f"SELECT post_id FROM items WHERE post_id IN ({marks})"
        f" UNION SELECT post_id FROM writes WHERE post_id IN ({marks})",
        (*wanted, *wanted),
    ).fetchall()
    return {str(r["post_id"]) for r in rows}


def history(conn: sqlite3.Connection, *, limit: int, account_alias: str | None) -> list[PlanRecord]:
    if account_alias is None:
        rows = conn.execute(
            "SELECT * FROM writes ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM writes WHERE account_alias = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (account_alias, limit),
        ).fetchall()
    return [with_items(conn, r) for r in rows]


def count(conn: sqlite3.Connection, *, account_alias: str | None) -> int:
    """How many rows ``history`` would match without its limit."""
    if account_alias is None:
        row = conn.execute("SELECT count(*) FROM writes").fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) FROM writes WHERE account_alias = ?", (account_alias,)
        ).fetchone()
    return int(row[0])


def last_published(conn: sqlite3.Connection, account_alias: str) -> PlanRecord | None:
    """The account's newest ``published`` row, filtered in SQL before the limit."""
    row = conn.execute(
        "SELECT * FROM writes WHERE account_alias = ? AND state = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (account_alias, PUBLISHED),
    ).fetchone()
    return None if row is None else with_items(conn, row)


def unresolved(conn: sqlite3.Connection, *, cutoff: str) -> list[PlanRecord]:
    rows = conn.execute(
        "SELECT w.* FROM writes w WHERE w.state = ?"
        " OR (w.state = ? AND (SELECT MAX(i.submitted_at) FROM items i"
        "     WHERE i.write_id = w.id) < ?)"
        " ORDER BY w.id",
        (UNKNOWN, SUBMITTING, cutoff),
    ).fetchall()
    return [with_items(conn, r) for r in rows]


def usage(
    conn: sqlite3.Connection,
    account_alias: str,
    *,
    day_start: datetime,
    month_start: datetime,
    exclude_write_id: int | None = None,
) -> Usage:
    """Spend over every account and posts for ``account_alias``, since each window start.

    Counts committed items (submitting, published, unknown) by their
    ``submitted_at``, reserved items (pending, in a row still open) by
    when they were claimed, and reads by when they were recorded.
    ``exclude_write_id`` leaves out the row being re-claimed, whose own
    items the caller is about to count as planned.
    """
    day, month = iso(day_start), iso(month_start)
    committed = ", ".join("?" for _ in COMMITTED_ITEM_STATES)
    open_rows = ", ".join("?" for _ in OPEN_ROW_STATES)
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN t >= ? THEN cost END), 0),"
        " COALESCE(SUM(CASE WHEN t >= ? THEN cost END), 0),"
        " COUNT(CASE WHEN t >= ? AND alias = ? THEN 1 END)"
        " FROM (SELECT i.est_cost_usd AS cost, w.account_alias AS alias,"
        "       CASE WHEN i.state = ? THEN i.updated_at ELSE i.submitted_at END AS t"
        "       FROM items i JOIN writes w ON w.id = i.write_id"
        f"      WHERE (i.state IN ({committed})"
        f"             OR (i.state = ? AND w.state IN ({open_rows})))"
        "       AND w.id IS NOT ?)"
        " WHERE t >= ?",
        (day, month, day, account_alias, PENDING, *COMMITTED_ITEM_STATES, PENDING,
         *OPEN_ROW_STATES, exclude_write_id, min(day, month)),
    ).fetchone()  # fmt: skip
    reads = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN created_at >= ? THEN est_cost_usd END), 0),"
        " COALESCE(SUM(CASE WHEN created_at >= ? THEN est_cost_usd END), 0)"
        " FROM reads WHERE created_at >= ?",
        (day, month, min(day, month)),
    ).fetchone()
    return Usage(
        spent_day_usd=round(float(row[0]) + float(reads[0]), 6),
        spent_month_usd=round(float(row[1]) + float(reads[1]), 6),
        posts_day=int(row[2]),
    )


def replied_to(conn: sqlite3.Connection, account_alias: str, wanted: Sequence[str]) -> set[str]:
    """Which of ``wanted`` the account has answered: a reply it published, or
    one that may have gone out (``submitting``, ``unknown``), counts."""
    if not wanted:
        return set()
    marks = ", ".join("?" for _ in wanted)
    states = ", ".join("?" for _ in COMMITTED_ITEM_STATES)
    rows = conn.execute(
        "SELECT DISTINCT i.reply_to FROM items i JOIN writes w ON w.id = i.write_id"
        f" WHERE w.account_alias = ? AND i.reply_to IN ({marks}) AND i.state IN ({states})",
        (account_alias, *wanted, *COMMITTED_ITEM_STATES),
    ).fetchall()
    return {str(r[0]) for r in rows}
