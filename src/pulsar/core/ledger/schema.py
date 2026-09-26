"""The ledger's schema, its migrations, and the transaction helpers every write uses.

``PRAGMA user_version`` carries the schema version; ``MIGRATIONS`` maps each
version to the script that brings the one before it up to date. Shipped
scripts are frozen: a schema change is a new entry appended here.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from collections.abc import Generator
from pathlib import Path

from ..errors import INVALID_CONFIG, PulsarError

log = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 10_000

# One row per logical write. Nullable columns are per-tool (a delete has no
# text, an upload no post). ``meta_json`` holds small non-secret, tool-specific
# facts (mime, bytes, processing_state, deleted) — never media bytes, text,
# or credentials. ``caller`` is self-asserted by the calling agent: audit, not
# identity.
SCHEMA_V1 = """
CREATE TABLE writes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    tool            TEXT    NOT NULL,
    account_user_id TEXT,
    account_handle  TEXT,
    caller          TEXT,
    request_digest  TEXT    NOT NULL,
    text_sha256     TEXT,
    state           TEXT    NOT NULL
                    CHECK (state IN ('submitting', 'published', 'failed', 'unknown')),
    post_id         TEXT,
    media_id        TEXT,
    url             TEXT,
    error_code      TEXT,
    error_message   TEXT,
    retryable       INTEGER,
    meta_json       TEXT    NOT NULL DEFAULT '{}',
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
CREATE INDEX writes_state ON writes (state);
CREATE INDEX writes_post_id ON writes (post_id);
CREATE INDEX writes_account ON writes (account_user_id, created_at);
"""

# v2: providers, account aliases, plans and threads. SQLite cannot alter a
# CHECK constraint, so ``writes`` is rebuilt (create, copy, drop, rename) to
# widen its states; ``items`` holds one row per post of a thread. Existing
# ``create_post`` rows are back-filled with the one item they describe.
SCHEMA_V2 = """
CREATE TABLE writes_v2 (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    tool            TEXT    NOT NULL,
    provider        TEXT,
    account_alias   TEXT,
    account_user_id TEXT,
    account_handle  TEXT,
    caller          TEXT,
    request_digest  TEXT    NOT NULL,
    plan_digest     TEXT,
    text_sha256     TEXT,
    state           TEXT    NOT NULL
                    CHECK (state IN ('pending', 'submitting', 'published', 'partial',
                                     'failed', 'unknown', 'skipped')),
    post_id         TEXT,
    media_id        TEXT,
    url             TEXT,
    error_code      TEXT,
    error_message   TEXT,
    retryable       INTEGER,
    note            TEXT,
    meta_json       TEXT    NOT NULL DEFAULT '{}',
    attempts        INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);
INSERT INTO writes_v2 (
    id, idempotency_key, tool, provider, account_alias, account_user_id, account_handle,
    caller, request_digest, text_sha256, state, post_id, media_id, url, error_code,
    error_message, retryable, meta_json, attempts, created_at, updated_at
)
SELECT
    id, idempotency_key, tool, 'x',
    CASE WHEN account_handle IS NULL THEN NULL ELSE 'x:' || lower(account_handle) END,
    account_user_id, account_handle, caller, request_digest, text_sha256, state, post_id,
    media_id, url, error_code, error_message, retryable, meta_json, attempts, created_at,
    updated_at
FROM writes;
DROP TABLE writes;
ALTER TABLE writes_v2 RENAME TO writes;
CREATE INDEX writes_state ON writes (state);
CREATE INDEX writes_post_id ON writes (post_id);
CREATE INDEX writes_account ON writes (account_user_id, created_at);
CREATE INDEX writes_alias ON writes (account_alias, created_at);
CREATE TABLE items (
    write_id        INTEGER NOT NULL REFERENCES writes (id),
    idx             INTEGER NOT NULL,
    state           TEXT    NOT NULL
                    CHECK (state IN ('pending', 'submitting', 'published', 'failed', 'unknown')),
    text_sha256     TEXT,
    fingerprint     TEXT,
    est_cost_usd    REAL    NOT NULL DEFAULT 0,
    post_id         TEXT,
    url             TEXT,
    media_ids_json  TEXT    NOT NULL DEFAULT '[]',
    error_code      TEXT,
    error_message   TEXT,
    retryable       INTEGER,
    submitted_at    TEXT,
    updated_at      TEXT    NOT NULL,
    PRIMARY KEY (write_id, idx)
);
CREATE INDEX items_state ON items (state);
CREATE INDEX items_submitted ON items (submitted_at);
INSERT INTO items (
    write_id, idx, state, text_sha256, est_cost_usd, post_id, url, error_code,
    error_message, retryable, submitted_at, updated_at
)
SELECT
    id, 0, state, text_sha256, 0, post_id, url, error_code, error_message, retryable,
    created_at, updated_at
FROM writes WHERE tool = 'create_post';
"""

# user_version N -> the script that brings a version N-1 database to N.
MIGRATIONS: dict[int, str] = {1: SCHEMA_V1, 2: SCHEMA_V2}
SCHEMA_VERSION = max(MIGRATIONS)


def is_busy(exc: sqlite3.Error) -> bool:
    """Another connection holds the lock: worth waiting for, not a failure.

    Decided from SQLite's result code (STD-02@2 §R10); the low byte is the
    primary code under an extended one such as ``SQLITE_BUSY_SNAPSHOT``.
    """
    code: int | None = getattr(exc, "sqlite_errorcode", None)
    return code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


def switch_to_wal(conn: sqlite3.Connection, busy_timeout_ms: int) -> str:
    """Switch to WAL. On a new file this needs an exclusive lock and SQLite
    does not apply the busy timeout to it, so a second process opening the
    same new ledger gets ``database is locked`` at once; retry until the
    busy timeout instead."""
    deadline = time.monotonic() + busy_timeout_ms / 1000
    while True:
        try:
            return str(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        except sqlite3.OperationalError as exc:
            if not is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def newer_than_supported(path: Path, version: int) -> PulsarError:
    """This pulsar must not write, migrate down or reinterpret a newer file
    (STD-03@2 §R10), and a long-running process must notice when a newer
    pulsar migrates the file under it, so every connection checks."""
    return PulsarError(
        INVALID_CONFIG,
        f"ledger {path} has schema v{version}, newer than this pulsar understands "
        f"(v{SCHEMA_VERSION}); upgrade pulsar (and restart any running pulsar server)",
        detail={"path": str(path), "schema_version": version, "supported": SCHEMA_VERSION},
    )


def needs_migration(path: Path, version: int) -> PulsarError:
    """A read-only open never migrates (STD-01@2 §R31); the operator does."""
    return PulsarError(
        INVALID_CONFIG,
        f"ledger {path} has schema v{version} and this pulsar reads v{SCHEMA_VERSION}; "
        "run `pulsar migrate --confirm` to upgrade it",
        detail={"path": str(path), "schema_version": version, "supported": SCHEMA_VERSION},
    )


def migrate(conn: sqlite3.Connection, path: Path, busy_timeout_ms: int) -> tuple[int, int]:
    """Switch to WAL and bring the file to ``SCHEMA_VERSION``, in one transaction:
    ``(from_version, to_version)``. Refuses a file newer than this pulsar."""
    mode = switch_to_wal(conn, busy_timeout_ms)
    if str(mode).lower() != "wal":
        log.warning("ledger journal_mode is %s, not wal", mode)
    with immediate(conn):
        version = user_version(conn)
        if version > SCHEMA_VERSION:
            raise newer_than_supported(path, version)
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[target].split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target}")
    return version, SCHEMA_VERSION


@contextlib.contextmanager
def immediate(conn: sqlite3.Connection) -> Generator[None]:
    """BEGIN IMMEDIATE ... COMMIT: take the write lock up front, so two
    processes claiming one key serialize instead of both reading "absent"."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
