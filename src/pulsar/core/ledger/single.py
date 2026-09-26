"""The single-request API the legacy tools use: ``claim``, then ``publish`` or ``fail``.

Each function runs inside the caller's ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, assert_never

from ..errors import INTERNAL, OutcomeUnknown, PulsarError
from ..jsonx import obj
from . import text
from .keys import conflict
from .queries import get_row, require_row
from .records import (
    LEGACY_ITEM_TOOLS,
    LEGACY_PROVIDER,
    REARMABLE_TOOLS,
    TERMINAL_STATES,
    State,
    WriteRecord,
)


def claim(
    conn: sqlite3.Connection,
    now: str,
    *,
    key: str,
    tool: str,
    digest: str,
    account: dict[str, str],
    caller: str | None,
    text_sha256: str | None,
    meta: dict[str, Any] | None,
    stale_before: str | None,
) -> WriteRecord:
    """``Ledger.claim`` inside an open transaction. ``stale_before`` is the
    stamp before which a rearmable ``submitting`` row counts as abandoned
    (None: never)."""
    user_id, handle = account.get("user_id"), account.get("username")
    alias = f"{LEGACY_PROVIDER}:{handle.lower()}" if handle else None
    meta_json = text.persisted_meta(meta or {})
    caller_text = text.persisted_text(caller)
    row = get_row(conn, key)
    if row is None:
        conn.execute(
            "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
            " account_user_id, account_handle, caller, request_digest, text_sha256,"
            " state, meta_json, attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key, tool, LEGACY_PROVIDER, alias, user_id, handle, caller_text, digest,
             text_sha256, State.SUBMITTING, meta_json, now, now),
        )  # fmt: skip
    else:
        existing = WriteRecord.from_row(row)
        if existing.request_digest != digest or existing.account_user_id != user_id:
            raise conflict(key, existing.state)
        state = existing.state
        match state:
            case State.PUBLISHED:
                return existing
            case State.FAILED:  # nothing reached X: send it again
                conn.execute(
                    "UPDATE writes SET state = ?, caller = ?, account_handle = ?,"
                    " account_alias = ?, meta_json = ?,"
                    " error_code = NULL, error_message = NULL, retryable = NULL,"
                    " attempts = attempts + 1, updated_at = ? WHERE idempotency_key = ?",
                    (State.SUBMITTING, caller_text, handle, alias, meta_json, now, key),
                )
            case State.SUBMITTING if _abandoned(existing, stale_before):
                # A sender that crashed mid-request left this row claimed, and
                # without items reconcile never sees it. Its tool has no
                # duplicate effect, so the retry takes the row over (STD-02@2
                # §R33) instead of refusing the key for good; the takeover is
                # recorded in ``note`` and ``attempts``.
                note = (
                    f"re-armed at {now}: attempt {existing.attempts} was still submitting "
                    f"(last updated {existing.updated_at}, before the stale cutoff "
                    f"{stale_before})"
                )
                conn.execute(
                    "UPDATE writes SET caller = ?, account_handle = ?, account_alias = ?,"
                    " meta_json = ?, note = ?, attempts = attempts + 1, updated_at = ?"
                    " WHERE idempotency_key = ?",
                    (caller_text, handle, alias, meta_json, text.persisted_text(note), now,
                     key),
                )  # fmt: skip
            case State.SUBMITTING | State.UNKNOWN:
                raise OutcomeUnknown(
                    f"an earlier attempt with this idempotency_key is {state}",
                    detail={"idempotency_key": key, "state": state},
                )
            case State.PENDING | State.PARTIAL | State.SKIPPED:  # a plan row's key
                raise conflict(key, state)
            case _:
                assert_never(state)
    row = require_row(conn, key)
    if tool in LEGACY_ITEM_TOOLS:
        conn.execute(
            "INSERT INTO items (write_id, idx, state, text_sha256, submitted_at,"
            " updated_at) VALUES (?, 0, ?, ?, ?, ?)"
            " ON CONFLICT (write_id, idx) DO UPDATE SET state = excluded.state,"
            " error_code = NULL, error_message = NULL, retryable = NULL,"
            " submitted_at = excluded.submitted_at, updated_at = excluded.updated_at",
            (row["id"], State.SUBMITTING, text_sha256, now, now),
        )
    return WriteRecord.from_row(row)


def _abandoned(existing: WriteRecord, stale_before: str | None) -> bool:
    return (
        stale_before is not None
        and existing.tool in REARMABLE_TOOLS
        and existing.updated_at < stale_before
    )


def settle(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    state: State,
    *,
    meta: dict[str, Any] | None,
    columns: dict[str, Any],
) -> WriteRecord:
    """``Ledger.publish`` / ``Ledger.fail`` inside an open transaction."""
    if state not in TERMINAL_STATES:
        raise PulsarError(
            INTERNAL,
            f"idempotency_key {key!r} cannot settle as {state}; only as "
            f"{', '.join(sorted(TERMINAL_STATES))}",
            detail={"idempotency_key": key, "state": state},
        )
    row = require_row(conn, key)
    merged = {**obj(json.loads(row["meta_json"] or "{}")), **(meta or {})}
    values = {k: v for k, v in columns.items() if v is not None}
    if "retryable" in values:
        values["retryable"] = int(values["retryable"])
    if "error_message" in values:
        values["error_message"] = text.persisted_text(values["error_message"])
    values.update(state=state, meta_json=text.persisted_meta(merged))
    values["updated_at"] = now
    assignments = ", ".join(f"{name} = ?" for name in values)
    conn.execute(
        f"UPDATE writes SET {assignments} WHERE idempotency_key = ?",
        (*values.values(), key),
    )
    if row["tool"] in LEGACY_ITEM_TOOLS:
        item_values = {
            k: values[k]
            for k in ("state", "post_id", "url", "error_code", "error_message", "retryable")
            if k in values
        }
        item_values["updated_at"] = values["updated_at"]
        item_assignments = ", ".join(f"{name} = ?" for name in item_values)
        conn.execute(
            f"UPDATE items SET {item_assignments} WHERE write_id = ? AND idx = 0",
            (*item_values.values(), row["id"]),
        )
    return WriteRecord.from_row(require_row(conn, key))
