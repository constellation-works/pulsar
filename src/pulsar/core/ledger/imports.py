"""Historic rows written by something other than pulsar (``pulsar import``)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from . import text
from .queries import load, require
from .records import PUBLISHED, SKIPPED, AccountRef, PlanRecord, iso


def record_import(
    conn: sqlite3.Connection,
    now: str,
    *,
    key: str,
    tool: str,
    digest: str,
    provider: str,
    account: AccountRef,
    caller: str | None,
    created_at: datetime,
    post_id: str | None,
    url: str | None,
    text_sha256: str | None,
    note: str | None,
    meta: dict[str, Any],
) -> tuple[bool, PlanRecord]:
    """``Ledger.record_import`` inside an open transaction."""
    when = iso(created_at)
    state = SKIPPED if post_id is None else PUBLISHED
    existing = load(conn, key)
    if existing is not None:
        return False, existing
    cur = conn.execute(
        "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
        " account_user_id, account_handle, caller, request_digest, text_sha256, state,"
        " post_id, url, note, meta_json, attempts, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
        (key, tool, provider, account.alias, account.user_id, account.handle,
         text.persisted_text(caller), digest, text_sha256, state, post_id, url,
         text.persisted_text(note), text.persisted_meta(meta), when, now),
    )  # fmt: skip
    if post_id is not None:
        conn.execute(
            "INSERT INTO items (write_id, idx, state, text_sha256, est_cost_usd,"
            " post_id, url, submitted_at, updated_at) VALUES (?, 0, ?, ?, 0, ?, ?, ?, ?)",
            (cur.lastrowid, PUBLISHED, text_sha256, post_id, url, when, now),
        )
    return True, require(conn, key)
