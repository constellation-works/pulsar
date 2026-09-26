"""The plan API: claim a thread, move its items one by one, derive the row.

Each function runs inside the caller's ``BEGIN IMMEDIATE`` transaction; the
caller exports whatever record a settling function returns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from ..errors import INVALID_ARGUMENT, OUTCOME_UNKNOWN, OutcomeUnknown, PulsarError
from ..usage import Usage
from .keys import conflict, request_digest
from .queries import item, load, require, usage, write_id
from .records import (
    FAILED,
    IMPORT_TOOL,
    PARTIAL,
    PENDING,
    PUBLISHED,
    RESOLVED_ABSENT,
    SKIP_TOOL,
    SKIPPED,
    SUBMITTING,
    UNKNOWN,
    AccountRef,
    ItemIntent,
    PlanRecord,
    derive_state,
)


def claim_plan(
    conn: sqlite3.Connection,
    now: str,
    *,
    key: str,
    tool: str,
    digest: str,
    provider: str,
    account: AccountRef,
    caller: str | None,
    items: Sequence[ItemIntent],
    admit: Callable[[Usage], None] | None,
    day_start: datetime,
    month_start: datetime,
) -> PlanRecord | OutcomeUnknown:
    """``Ledger.claim_plan`` inside an open transaction. An ``OutcomeUnknown``
    is returned, not raised, so the fingerprints it back-fills commit first."""
    existing = load(conn, key)
    if existing is None:
        if admit is not None:
            admit(usage(conn, account.alias, day_start=day_start, month_start=month_start))
        cur = conn.execute(
            "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
            " account_user_id, account_handle, caller, request_digest, plan_digest,"
            " text_sha256, state, attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key, tool, provider, account.alias, account.user_id, account.handle,
             caller, digest, digest, items[0].text_sha256, PENDING, now, now),
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO items (write_id, idx, state, text_sha256, fingerprint,"
            " est_cost_usd, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (cur.lastrowid, idx, PENDING, it.text_sha256, it.fingerprint,
                 float(it.est_cost_usd), now)
                for idx, it in enumerate(items)
            ],
        )  # fmt: skip
        return require(conn, key)
    if existing.account_alias != account.alias:
        raise conflict(key, existing.state)
    if existing.state == SKIPPED:
        return existing
    # An imported row carries no plan digest (the routine that wrote it
    # kept no request), so the key and account alone identify it.
    if existing.tool != IMPORT_TOOL and (
        existing.tool != tool or existing.request_digest != digest
    ):
        raise conflict(key, existing.state)
    if existing.state == PUBLISHED:
        return existing
    row_id = write_id(conn, key)
    in_flight = any(i.state == SUBMITTING for i in existing.items)
    if existing.state in (SUBMITTING, UNKNOWN) or in_flight:
        # Same request, so the intents describe these very posts: give
        # rows migrated from v1 (no fingerprint) one, so reconcile can
        # match them. Committed before the refusal is raised.
        conn.executemany(
            "UPDATE items SET fingerprint = ? WHERE write_id = ? AND idx = ?"
            " AND fingerprint IS NULL",
            [(it.fingerprint, row_id, idx) for idx, it in enumerate(items)],
        )
        state = SUBMITTING if in_flight else existing.state
        return OutcomeUnknown(
            f"an earlier attempt with this idempotency_key is {state}",
            detail={"idempotency_key": key, "state": state},
        )
    if admit is not None:
        admit(
            usage(
                conn,
                account.alias,
                day_start=day_start,
                month_start=month_start,
                exclude_write_id=row_id,
            )
        )
    # Pending and failed items (re)take the current intent: its
    # fingerprint and price, and now as their reservation time.
    conn.executemany(
        "UPDATE items SET state = ?, text_sha256 = ?, fingerprint = ?,"
        " est_cost_usd = ?, error_code = NULL, error_message = NULL,"
        " retryable = NULL, submitted_at = NULL, updated_at = ?"
        " WHERE write_id = ? AND idx = ? AND state IN (?, ?)",
        [
            (PENDING, it.text_sha256, it.fingerprint, float(it.est_cost_usd), now,
             row_id, idx, PENDING, FAILED)
            for idx, it in enumerate(items)
        ],
    )  # fmt: skip
    if existing.state == PENDING:
        conn.execute(
            "UPDATE writes SET caller = ?, updated_at = ? WHERE id = ?",
            (caller, now, row_id),
        )
    else:  # failed or partial: nothing is in flight; re-send what did not go out
        conn.execute(
            "UPDATE writes SET state = ?, caller = ?, error_code = NULL,"
            " error_message = NULL, retryable = NULL, attempts = attempts + 1,"
            " updated_at = ? WHERE id = ?",
            (PENDING, caller, now, row_id),
        )
    return require(conn, key)


def begin_item(conn: sqlite3.Connection, now: str, key: str, idx: int) -> None:
    """``Ledger.begin_item`` inside an open transaction."""
    record = require(conn, key)
    current = item(record, idx)
    if record.state not in (PENDING, SUBMITTING) or current.state != PENDING:
        state = record.state if record.state != PENDING else current.state
        raise OutcomeUnknown(
            f"post {idx} of this idempotency_key is {current.state} (row {record.state}); "
            "another call may be sending it",
            detail={"idempotency_key": key, "state": state, "idx": idx},
        )
    if any(i.state != PUBLISHED for i in record.items[:idx]):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"post {idx} cannot start before the posts ahead of it are published",
            detail={"idempotency_key": key, "idx": idx},
        )
    row_id = write_id(conn, key)
    cur = conn.execute(
        "UPDATE items SET state = ?, submitted_at = ?, updated_at = ?"
        " WHERE write_id = ? AND idx = ? AND state = ?",
        (SUBMITTING, now, now, row_id, idx, PENDING),
    )
    assert cur.rowcount == 1  # the IMMEDIATE lock makes the check above the CAS
    conn.execute(
        "UPDATE writes SET state = ?, updated_at = ? WHERE id = ?",
        (SUBMITTING, now, row_id),
    )


def item_sending(conn: sqlite3.Connection, now: str, key: str, idx: int, stamp: str) -> bool:
    """``Ledger.item_sending`` inside an open transaction: True if re-stamped."""
    cur = conn.execute(
        "UPDATE items SET submitted_at = ?, updated_at = ?"
        " WHERE write_id = (SELECT id FROM writes WHERE idempotency_key = ?)"
        " AND idx = ? AND state = ? AND submitted_at = ?",
        (now, now, key, idx, SUBMITTING, stamp),
    )
    return cur.rowcount == 1


def set_item(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    idx: int,
    state: str,
    *,
    allowed_from: tuple[str, ...],
    columns: dict[str, Any],
) -> None:
    """Move item ``idx`` to ``state`` if it is in one of ``allowed_from``."""
    record = require(conn, key)
    current = item(record, idx)
    if current.state not in allowed_from:
        raise ValueError(
            f"{key!r} post {idx} is {current.state}; cannot move it to {state}"
            f" (only from {', '.join(allowed_from)})"
        )
    values = {**columns, "state": state, "updated_at": now}
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE items SET {assignments} WHERE write_id = ? AND idx = ?",
        (*values.values(), write_id(conn, key), idx),
    )


def finish(conn: sqlite3.Connection, now: str, key: str) -> PlanRecord:
    """``Ledger.finish`` inside an open transaction; the caller exports."""
    record = require(conn, key)
    if record.state == SKIPPED or not record.items:
        raise ValueError(f"{key!r} is {record.state} with no posts; nothing to finish")
    row_id = write_id(conn, key)
    conn.execute(
        "UPDATE items SET state = ?, error_code = ?, error_message = ?, retryable = 0,"
        " updated_at = ? WHERE write_id = ? AND state = ?",
        (UNKNOWN, OUTCOME_UNKNOWN, "no outcome was recorded for this post", now,
         row_id, SUBMITTING),
    )  # fmt: skip
    record = require(conn, key)
    state = derive_state(record.items)
    first = record.items[0]
    culprit = next((i for i in record.items if i.state != PUBLISHED and i.error_code), None)
    conn.execute(
        "UPDATE writes SET state = ?, post_id = ?, url = ?, error_code = ?,"
        " error_message = ?, retryable = ?, updated_at = ? WHERE id = ?",
        (
            state,
            first.post_id,
            first.url,
            None if state == PUBLISHED or culprit is None else culprit.error_code,
            None if state == PUBLISHED or culprit is None else culprit.error_message,
            None
            if state == PUBLISHED or culprit is None or culprit.retryable is None
            else int(culprit.retryable),
            now,
            row_id,
        ),
    )
    return require(conn, key)


def settle(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    *,
    seen: Mapping[int, tuple[str, str | None]],
    verdicts: Mapping[int, tuple[str, str | None] | None],
) -> PlanRecord | None:
    """``Ledger.settle`` inside an open transaction; None if the row changed."""
    record = require(conn, key)
    open_now = {
        i.idx: (i.state, i.submitted_at) for i in record.items if i.state in (UNKNOWN, SUBMITTING)
    }
    if record.state not in (UNKNOWN, SUBMITTING) or open_now != dict(seen):
        return None
    row_id = write_id(conn, key)
    for idx, verdict in verdicts.items():
        if verdict is None:
            values: dict[str, Any] = {
                "state": FAILED,
                "error_code": RESOLVED_ABSENT,
                "error_message": (
                    "reconcile found no such post on the account; it was not published"
                ),
                "retryable": 1,
            }
        else:
            values = {
                "state": PUBLISHED,
                "post_id": verdict[0],
                "url": verdict[1],
                "error_code": None,
                "error_message": None,
                "retryable": None,
            }
        values["updated_at"] = now
        assignments = ", ".join(f"{name} = ?" for name in values)
        conn.execute(
            f"UPDATE items SET {assignments} WHERE write_id = ? AND idx = ?",
            (*values.values(), row_id, idx),
        )
    return finish(conn, now, key)


def skip(
    conn: sqlite3.Connection,
    now: str,
    *,
    key: str,
    provider: str,
    account: AccountRef,
    caller: str | None,
    note: str | None,
) -> tuple[bool, PlanRecord]:
    """``Ledger.skip`` inside an open transaction: ``(changed, row)``."""
    existing = load(conn, key)
    if existing is None:
        conn.execute(
            "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
            " account_user_id, account_handle, caller, request_digest, state, note,"
            " attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key, SKIP_TOOL, provider, account.alias, account.user_id, account.handle,
             caller, request_digest(SKIP_TOOL, key=key), SKIPPED, note, now, now),
        )  # fmt: skip
    else:
        if existing.account_alias != account.alias:
            raise conflict(key, existing.state)
        if existing.state == SKIPPED:
            return False, existing
        if existing.state not in (PENDING, FAILED, PARTIAL) or any(
            i.state == SUBMITTING for i in existing.items
        ):
            raise conflict(key, existing.state)
        conn.execute(
            "UPDATE writes SET state = ?, note = ?, caller = ?, updated_at = ?"
            " WHERE idempotency_key = ?",
            (SKIPPED, note, caller, now, key),
        )
    return True, require(conn, key)
