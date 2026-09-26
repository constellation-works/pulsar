"""Free text reaches the ledger file only through the one redaction hook (STD-05@1 §R13).

``REDACTED_COLUMNS`` is the inventory. These tests hold it to the schema (every
TEXT column is either in it or known to be structured) and to the writers
(every writer of those columns goes through the hook).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

import pulsar.core.ledger.text as text_mod
from pulsar.core.errors import OutcomeUnknown, PulsarError
from pulsar.core.ledger import Ledger
from pulsar.core.ledger.text import REDACTED_COLUMNS
from pulsar.core.writelog import WriteLog

from .test_ledger import ACCT, ME, claim_plan, sql

# TEXT columns that hold structured values: keys, digests, hashes, provider
# ids, urls, codes, states and timestamps. Nothing a caller writes in prose.
STRUCTURED = {
    "writes.idempotency_key",  # validated by check_key, secret-scanned
    "writes.tool",
    "writes.provider",
    "writes.account_alias",
    "writes.account_user_id",
    "writes.account_handle",
    "writes.request_digest",
    "writes.plan_digest",
    "writes.text_sha256",
    "writes.state",
    "writes.post_id",
    "writes.media_id",
    "writes.url",
    "writes.error_code",
    "writes.created_at",
    "writes.updated_at",
    "items.state",
    "items.text_sha256",
    "items.fingerprint",
    "items.post_id",
    "items.url",
    "items.media_ids_json",
    "items.error_code",
    "items.submitted_at",
    "items.updated_at",
}


def test_every_text_column_is_either_redacted_or_structured(paths):
    Ledger(paths).migrate()
    columns = {
        f"{table}.{name}"
        for table in ("writes", "items")
        for _, name, kind, *_ in sql(paths, f"PRAGMA table_info({table})")
        if kind == "TEXT"
    }
    assert set(REDACTED_COLUMNS).isdisjoint(STRUCTURED)
    assert columns == set(REDACTED_COLUMNS) | STRUCTURED


@pytest.fixture
def marked(monkeypatch):
    """Swap the hook for one that brackets what it lets through."""
    monkeypatch.setattr(
        text_mod, "persisted_text", lambda value: None if value is None else f"[{value}]"
    )


def test_every_writer_of_free_text_goes_through_the_hook(paths, marked):
    ledger = Ledger(paths)
    # Legacy single-request rows: caller, meta, error message.
    ledger.claim(key="up", tool="upload_media", digest="u", account=ME, caller="bot",
                 meta={"mime": "image/png", "nested": {"why": "free text"}})  # fmt: skip
    ledger.fail("up", PulsarError("invalid_media", "X said: codec"), meta={"state": "failed"})
    # Plan rows: caller, item and row error messages.
    claim_plan(ledger, "thread", n=2)
    ledger.begin_item("thread", 0)
    ledger.item_unknown("thread", 0, OutcomeUnknown("ReadTimeout"))
    ledger.finish("thread")
    # Skip and import: note, caller, meta.
    ledger.skip(key="s", provider="x", account=ACCT, caller="bot", note="not now")
    ledger.record_import(
        key="i", tool="import:posted.jsonl", digest="d", provider="x", account=ACCT,
        caller="importer", created_at=datetime(2026, 9, 1, tzinfo=UTC), post_id="1", url="u",
        text_sha256=None, note="historic", meta={"superseded_note": "old words"},
    )  # fmt: skip

    for column in REDACTED_COLUMNS:
        table, name = column.split(".")
        values = [v for (v,) in sql(paths, f"SELECT {name} FROM {table}") if v is not None]
        if name == "meta_json":
            values = [s for v in values for s in strings(json.loads(v))]
        assert values, f"no writer exercised {column}"
        unmarked = [v for v in values if not (v.startswith("[") and v.endswith("]"))]
        assert unmarked == [], f"{column} was written around the hook"


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


# A credential shape a provider could echo back in an error body.
LEAKED = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
LEAKED_VALUE = LEAKED.rsplit(" ", 1)[1]


def stored_bytes(paths) -> bytes:
    raw = paths.ledger_db.read_bytes()
    for wal in paths.home.glob("ledger.sqlite3-wal"):
        raw += wal.read_bytes()
    return raw


def test_a_credential_in_an_error_message_is_stored_masked(paths):
    ledger = Ledger(paths)
    ledger.claim(key="up", tool="upload_media", digest="u", account=ME, caller="bot",
                 meta={"echo": LEAKED})  # fmt: skip
    ledger.fail("up", PulsarError("provider_error", f"X said: {LEAKED}"))
    claim_plan(ledger, "thread", n=1)
    ledger.begin_item("thread", 0)
    ledger.item_unknown("thread", 0, OutcomeUnknown(f"reset after {LEAKED}"))
    ledger.finish("thread")

    assert LEAKED_VALUE.encode() not in stored_bytes(paths)
    (message,) = [v for (v,) in sql(paths, "SELECT error_message FROM writes WHERE "
                                           "idempotency_key = 'up'")]  # fmt: skip
    assert "[redacted:bearer header]" in message


def test_a_credential_never_reaches_the_export(paths):
    log = WriteLog(paths)
    log.append(tool="publish", caller=f"bot {LEAKED}", extra={"detail": {"why": [LEAKED]}})
    line = paths.write_log.read_text()
    assert LEAKED_VALUE not in line
    assert json.loads(line)["detail"]["why"] == ["Authorization: Bearer [redacted:bearer header]"]
