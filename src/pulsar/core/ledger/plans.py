"""The plan API: claim a thread, move its items one by one, derive the row.

Each function runs inside the caller's ``BEGIN IMMEDIATE`` transaction; the
caller exports whatever record a settling function returns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, assert_never

from ..errors import INTERNAL, INVALID_ARGUMENT, OUTCOME_UNKNOWN, OutcomeUnknown, PulsarError
from ..usage import Usage
from . import text
from .keys import conflict, request_digest
from .queries import item, load, require, usage, write_id
from .records import (
    IMPORT_TOOL,
    RESOLVED_ABSENT,
    SKIP_TOOL,
    AccountRef,
    ItemIntent,
    PlanRecord,
    State,
    derive_state,
    is_ambiguous,
    is_open,
)

# What ``finish`` records on an item that was still submitting: no outcome.
_NO_OUTCOME = "no outcome was recorded for this post"
_ABSENT = "reconcile found no such post on the account; it was not published"


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
    caller_text = text.persisted_text(caller)
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
             caller_text, digest, digest, items[0].text_sha256, State.PENDING, now, now),
        )  # fmt: skip
        conn.executemany(
            "INSERT INTO items (write_id, idx, state, text_sha256, fingerprint,"
            " est_cost_usd, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (cur.lastrowid, idx, State.PENDING, it.text_sha256, it.fingerprint,
                 float(it.est_cost_usd), now)
                for idx, it in enumerate(items)
            ],
        )  # fmt: skip
        return require(conn, key)
    if existing.account_alias != account.alias:
        raise conflict(key, existing.state)
    if existing.state is State.SKIPPED:
        return existing
    # An imported row carries no plan digest (the routine that wrote it
    # kept no request), so the key and account alone identify it.
    if existing.tool != IMPORT_TOOL and (
        existing.tool != tool or existing.request_digest != digest
    ):
        raise conflict(key, existing.state)
    row_id = write_id(conn, key)
    in_flight = any(i.state is State.SUBMITTING for i in existing.items)
    state = existing.state
    match state:  # skipped returned above, before the request was compared
        case State.PUBLISHED:
            return existing
        case State.SUBMITTING | State.UNKNOWN:
            return _blocked(conn, key, row_id, items, State.SUBMITTING if in_flight else state)
        case State.PENDING | State.FAILED | State.PARTIAL:
            if in_flight:
                return _blocked(conn, key, row_id, items, State.SUBMITTING)
        case _:
            assert_never(state)
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
            (State.PENDING, it.text_sha256, it.fingerprint, float(it.est_cost_usd), now,
             row_id, idx, State.PENDING, State.FAILED)
            for idx, it in enumerate(items)
        ],
    )  # fmt: skip
    if state is State.PENDING:
        conn.execute(
            "UPDATE writes SET caller = ?, updated_at = ? WHERE id = ?",
            (caller_text, now, row_id),
        )
    else:  # failed or partial: nothing is in flight; re-send what did not go out
        conn.execute(
            "UPDATE writes SET state = ?, caller = ?, error_code = NULL,"
            " error_message = NULL, retryable = NULL, attempts = attempts + 1,"
            " updated_at = ? WHERE id = ?",
            (State.PENDING, caller_text, now, row_id),
        )
    return require(conn, key)


def _blocked(
    conn: sqlite3.Connection, key: str, row_id: int, items: Sequence[ItemIntent], state: State
) -> OutcomeUnknown:
    # Same request, so the intents describe these very posts: give rows
    # migrated from v1 (no fingerprint) one, so reconcile can match them.
    # Committed before the refusal is raised.
    conn.executemany(
        "UPDATE items SET fingerprint = ? WHERE write_id = ? AND idx = ? AND fingerprint IS NULL",
        [(it.fingerprint, row_id, idx) for idx, it in enumerate(items)],
    )
    return OutcomeUnknown(
        f"an earlier attempt with this idempotency_key is {state}",
        detail={"idempotency_key": key, "state": state},
    )


def begin_item(conn: sqlite3.Connection, now: str, key: str, idx: int) -> None:
    """``Ledger.begin_item`` inside an open transaction."""
    record = require(conn, key)
    current = item(record, idx)
    if not is_open(record.state) or current.state is not State.PENDING:
        state = record.state if record.state is not State.PENDING else current.state
        raise OutcomeUnknown(
            f"post {idx} of this idempotency_key is {current.state} (row {record.state}); "
            "another call may be sending it",
            detail={"idempotency_key": key, "state": state, "idx": idx},
        )
    if any(i.state is not State.PUBLISHED for i in record.items[:idx]):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"post {idx} cannot start before the posts ahead of it are published",
            detail={"idempotency_key": key, "idx": idx},
        )
    row_id = write_id(conn, key)
    cur = conn.execute(
        "UPDATE items SET state = ?, submitted_at = ?, updated_at = ?"
        " WHERE write_id = ? AND idx = ? AND state = ?",
        (State.SUBMITTING, now, now, row_id, idx, State.PENDING),
    )
    if cur.rowcount != 1:
        # The IMMEDIATE lock makes the check above the compare-and-set, so
        # this cannot match nothing unless that lock was not held.
        raise PulsarError(
            INTERNAL,
            f"compare-and-set of post {idx} of idempotency_key {key!r} to submitting matched "
            f"{cur.rowcount} rows under the write lock",
            detail={"idempotency_key": key, "idx": idx},
        )
    conn.execute(
        "UPDATE writes SET state = ?, updated_at = ? WHERE id = ?",
        (State.SUBMITTING, now, row_id),
    )


def item_sending(conn: sqlite3.Connection, now: str, key: str, idx: int, stamp: str) -> bool:
    """``Ledger.item_sending`` inside an open transaction: True if re-stamped."""
    cur = conn.execute(
        "UPDATE items SET submitted_at = ?, updated_at = ?"
        " WHERE write_id = (SELECT id FROM writes WHERE idempotency_key = ?)"
        " AND idx = ? AND state = ? AND submitted_at = ?",
        (now, now, key, idx, State.SUBMITTING, stamp),
    )
    return cur.rowcount == 1


def set_item(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    idx: int,
    state: State,
    *,
    allowed_from: tuple[State, ...],
    columns: dict[str, Any],
) -> None:
    """Move item ``idx`` to ``state`` if it is in one of ``allowed_from``."""
    record = require(conn, key)
    current = item(record, idx)
    if current.state not in allowed_from:
        raise PulsarError(
            INTERNAL,
            f"post {idx} of idempotency_key {key!r} is {current.state}; cannot move it to "
            f"{state} (only from {', '.join(allowed_from)})",
            detail={"idempotency_key": key, "idx": idx, "state": current.state},
        )
    values = {**columns, "state": state, "updated_at": now}
    if "error_message" in values:
        values["error_message"] = text.persisted_text(values["error_message"])
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE items SET {assignments} WHERE write_id = ? AND idx = ?",
        (*values.values(), write_id(conn, key), idx),
    )


def finish(conn: sqlite3.Connection, now: str, key: str) -> PlanRecord:
    """``Ledger.finish`` inside an open transaction; the caller exports."""
    record = require(conn, key)
    if record.state is State.SKIPPED or not record.items:
        raise PulsarError(
            INTERNAL,
            f"idempotency_key {key!r} is {record.state} with no posts; nothing to finish",
            detail={"idempotency_key": key, "state": record.state},
        )
    row_id = write_id(conn, key)
    conn.execute(
        "UPDATE items SET state = ?, error_code = ?, error_message = ?, retryable = 0,"
        " updated_at = ? WHERE write_id = ? AND state = ?",
        (State.UNKNOWN, OUTCOME_UNKNOWN, text.persisted_text(_NO_OUTCOME), now, row_id,
         State.SUBMITTING),
    )  # fmt: skip
    record = require(conn, key)
    state = derive_state(record.items)
    first = record.items[0]
    culprit = next(
        (i for i in record.items if i.state is not State.PUBLISHED and i.error_code), None
    )
    blame = None if state is State.PUBLISHED else culprit
    conn.execute(
        "UPDATE writes SET state = ?, post_id = ?, url = ?, error_code = ?,"
        " error_message = ?, retryable = ?, updated_at = ? WHERE id = ?",
        (
            state,
            first.post_id,
            first.url,
            None if blame is None else blame.error_code,
            None if blame is None else text.persisted_text(blame.error_message),
            None if blame is None or blame.retryable is None else int(blame.retryable),
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
    open_now = {i.idx: (i.state, i.submitted_at) for i in record.items if is_ambiguous(i.state)}
    if not is_ambiguous(record.state) or open_now != dict(seen):
        return None
    if stray := sorted(set(verdicts) - set(open_now)):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"reconcile has verdicts for posts {stray} of idempotency_key {key!r}, which are "
            "not open (only unknown or submitting posts are reconcile's to settle)",
            detail={"idempotency_key": key, "idx": stray},
        )
    row_id = write_id(conn, key)
    for idx, verdict in verdicts.items():
        if verdict is None:
            values: dict[str, Any] = {
                "state": State.FAILED,
                "error_code": RESOLVED_ABSENT,
                "error_message": text.persisted_text(_ABSENT),
                "retryable": 1,
            }
        else:
            values = {
                "state": State.PUBLISHED,
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
    caller_text, note_text = text.persisted_text(caller), text.persisted_text(note)
    existing = load(conn, key)
    if existing is None:
        conn.execute(
            "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
            " account_user_id, account_handle, caller, request_digest, state, note,"
            " attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key, SKIP_TOOL, provider, account.alias, account.user_id, account.handle,
             caller_text, request_digest(SKIP_TOOL, key=key), State.SKIPPED, note_text, now,
             now),
        )  # fmt: skip
        return True, require(conn, key)
    if existing.account_alias != account.alias:
        raise conflict(key, existing.state)
    state = existing.state
    match state:
        case State.SKIPPED:
            return False, existing
        case State.PENDING | State.FAILED | State.PARTIAL:
            # Nothing of it is in flight unless an item is: then it is not ours to stop.
            if any(i.state is State.SUBMITTING for i in existing.items):
                raise conflict(key, state)
        case State.SUBMITTING | State.PUBLISHED | State.UNKNOWN:
            raise conflict(key, state)
        case _:
            assert_never(state)
    conn.execute(
        "UPDATE writes SET state = ?, note = ?, caller = ?, updated_at = ?"
        " WHERE idempotency_key = ?",
        (State.SKIPPED, note_text, caller_text, now, key),
    )
    return True, require(conn, key)
