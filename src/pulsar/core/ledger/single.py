"""The single-request API the legacy tools use: ``claim``, then ``publish`` or ``fail``.

Each function runs inside the caller's ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..errors import OutcomeUnknown
from ..jsonx import obj
from .keys import conflict
from .queries import get_row
from .records import (
    LEGACY_ITEM_TOOLS,
    LEGACY_PROVIDER,
    PUBLISHED,
    SUBMITTING,
    TERMINAL_STATES,
    UNKNOWN,
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
) -> WriteRecord:
    """``Ledger.claim`` inside an open transaction."""
    user_id, handle = account.get("user_id"), account.get("username")
    alias = f"{LEGACY_PROVIDER}:{handle.lower()}" if handle else None
    meta_json = json.dumps(meta or {}, sort_keys=True)
    row = get_row(conn, key)
    if row is None:
        conn.execute(
            "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
            " account_user_id, account_handle, caller, request_digest, text_sha256,"
            " state, meta_json, attempts, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (key, tool, LEGACY_PROVIDER, alias, user_id, handle, caller, digest,
             text_sha256, SUBMITTING, meta_json, now, now),
        )  # fmt: skip
    else:
        existing = WriteRecord.from_row(row)
        if (
            existing.request_digest != digest
            or existing.account_user_id != user_id
            or existing.state not in (TERMINAL_STATES | {SUBMITTING})
        ):
            raise conflict(key, existing.state)
        if existing.state == PUBLISHED:
            return existing
        if existing.state in (SUBMITTING, UNKNOWN):
            raise OutcomeUnknown(
                f"an earlier attempt with this idempotency_key is {existing.state}",
                detail={"idempotency_key": key, "state": existing.state},
            )
        conn.execute(
            "UPDATE writes SET state = ?, caller = ?, account_handle = ?,"
            " account_alias = ?, meta_json = ?,"
            " error_code = NULL, error_message = NULL, retryable = NULL,"
            " attempts = attempts + 1, updated_at = ? WHERE idempotency_key = ?",
            (SUBMITTING, caller, handle, alias, meta_json, now, key),
        )
    row = get_row(conn, key)
    assert row is not None
    if tool in LEGACY_ITEM_TOOLS:
        conn.execute(
            "INSERT INTO items (write_id, idx, state, text_sha256, submitted_at,"
            " updated_at) VALUES (?, 0, ?, ?, ?, ?)"
            " ON CONFLICT (write_id, idx) DO UPDATE SET state = excluded.state,"
            " error_code = NULL, error_message = NULL, retryable = NULL,"
            " submitted_at = excluded.submitted_at, updated_at = excluded.updated_at",
            (row["id"], SUBMITTING, text_sha256, now, now),
        )
    return WriteRecord.from_row(row)


def settle(
    conn: sqlite3.Connection,
    now: str,
    key: str,
    state: str,
    *,
    meta: dict[str, Any] | None,
    columns: dict[str, Any],
) -> WriteRecord:
    """``Ledger.publish`` / ``Ledger.fail`` inside an open transaction."""
    assert state in TERMINAL_STATES
    row = get_row(conn, key)
    if row is None:
        raise KeyError(key)
    merged = {**obj(json.loads(row["meta_json"] or "{}")), **(meta or {})}
    values = {k: v for k, v in columns.items() if v is not None}
    if "retryable" in values:
        values["retryable"] = int(values["retryable"])
    values.update(state=state, meta_json=json.dumps(merged, sort_keys=True))
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
    row = get_row(conn, key)
    assert row is not None
    return WriteRecord.from_row(row)
