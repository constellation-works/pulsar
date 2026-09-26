"""The write ledger: one SQLite row per logical write, committed before it leaves.

Every post costs money and X has no idempotency key of its own, so pulsar
keeps one. A write is *claimed* — its row inserted as ``submitting`` and
committed — before the request goes out, and *settled* afterwards:

    submitting ──> published   X confirmed; later calls with the key replay it
               ├─> failed      X or the network proved nothing happened; retry allowed
               └─> unknown     the request may have reached X; never re-sent

A crash, a cancelled call or a timeout therefore leaves a row behind instead
of nothing, and a second call with the same key cannot post twice: it gets
the stored receipt, an ``idempotency_conflict``, or ``outcome_unknown``.

The file is SQLite in WAL mode, created 0600 before SQLite opens it, and
opened per operation with a busy timeout so several pulsar processes can
share one home. ``PRAGMA user_version`` carries the schema version; phase 2
migrates it (accounts, plans, threads, approvals) in ``_MIGRATIONS``.

``writes.jsonl`` is an export: every terminal transition appends one line
there through ``WriteLog.export``. The ledger is the source of truth.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import unicodedata
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .config import Paths
from .errors import (
    IDEMPOTENCY_CONFLICT,
    INVALID_ARGUMENT,
    INVALID_CONFIG,
    OUTCOME_UNKNOWN,
    SECRET_DETECTED,
    OutcomeUnknown,
    PulsarError,
)
from .fsutil import append_private
from .guard import scan_for_secrets

log = logging.getLogger(__name__)

SUBMITTING = "submitting"
PUBLISHED = "published"
FAILED = "failed"
UNKNOWN = "unknown"
TERMINAL_STATES = frozenset({PUBLISHED, FAILED, UNKNOWN})

MAX_KEY_LENGTH = 200
BUSY_TIMEOUT_MS = 10_000

# One row per logical write. Nullable columns are per-tool (a delete has no
# text, an upload no post). ``meta_json`` holds small non-secret, tool-specific
# facts (mime, bytes, processing_state, deleted) — never media bytes, text,
# or credentials. ``caller`` is self-asserted by the calling agent: audit, not
# identity.
_SCHEMA_V1 = """
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

# user_version N -> the script that brings a version N-1 database to N.
_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1}
SCHEMA_VERSION = max(_MIGRATIONS)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def request_digest(tool: str, **fields: Any) -> str:
    """SHA-256 of the canonical JSON of what the caller asked for.

    Two calls with one idempotency key must be the same request; comparing
    digests catches a caller reusing a key for different text.
    """
    blob = json.dumps(
        {"tool": tool, **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_key(digest: str, user_id: str) -> str:
    """Key for callers that pass none: the same request as the same account."""
    return hashlib.sha256(f"{digest}:{user_id}".encode()).hexdigest()


def check_key(key: object) -> str | None:
    """Validate a caller-supplied idempotency key (``None`` means derive one)."""
    if key is None:
        return None
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= MAX_KEY_LENGTH
        or any(c.isspace() or unicodedata.category(c).startswith("C") for c in key)
    ):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"idempotency_key must be 1-{MAX_KEY_LENGTH} characters with no whitespace "
            "or control characters",
        )
    if scan_for_secrets(key):
        # It is stored and exported verbatim; never let a credential in that way.
        raise PulsarError(
            SECRET_DETECTED, "idempotency_key looks like a credential; use an opaque id"
        )
    return key


@dataclass(frozen=True)
class WriteRecord:
    idempotency_key: str
    tool: str
    state: str
    request_digest: str
    account_user_id: str | None = None
    account_handle: str | None = None
    caller: str | None = None
    text_sha256: str | None = None
    post_id: str | None = None
    media_id: str | None = None
    url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> WriteRecord:
        return cls(
            idempotency_key=row["idempotency_key"],
            tool=row["tool"],
            state=row["state"],
            request_digest=row["request_digest"],
            account_user_id=row["account_user_id"],
            account_handle=row["account_handle"],
            caller=row["caller"],
            text_sha256=row["text_sha256"],
            post_id=row["post_id"],
            media_id=row["media_id"],
            url=row["url"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=None if row["retryable"] is None else bool(row["retryable"]),
            meta=json.loads(row["meta_json"] or "{}"),
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class Ledger:
    def __init__(
        self,
        paths: Paths,
        *,
        export: Callable[[WriteRecord], Any] | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        self.paths = paths
        self._export = export
        self._busy_timeout_ms = busy_timeout_ms
        self._ready = False

    # -- connection ---------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """A short-lived connection in autocommit mode; callers BEGIN explicitly.

        Opening per operation keeps transactions short and lets several
        processes share the file; the busy timeout makes them queue rather
        than fail.
        """
        path = self.paths.ledger_db
        self.paths.ensure()
        # Create the file 0600 ourselves so SQLite never creates it with the
        # process umask; its -wal/-shm files inherit the database's mode.
        append_private(path, "")
        conn = sqlite3.connect(path, timeout=self._busy_timeout_ms / 1000, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self._busy_timeout_ms)}")
            # The submitting row must survive a power cut before the POST goes out.
            conn.execute("PRAGMA synchronous = FULL")
            if not self._ready:
                self._migrate(conn)
                self._ready = True
            yield conn
        finally:
            conn.close()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            log.warning("ledger journal_mode is %s, not wal", mode)
        with _immediate(conn):
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise PulsarError(
                    INVALID_CONFIG,
                    f"ledger schema v{version} is newer than this pulsar (v{SCHEMA_VERSION}); "
                    "upgrade pulsar",
                )
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[target].split(";"):
                    if statement.strip():
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")

    # -- reads --------------------------------------------------------------

    def get(self, key: str) -> WriteRecord | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
        return None if row is None else WriteRecord.from_row(row)

    def all(self) -> list[WriteRecord]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM writes ORDER BY id").fetchall()
        return [WriteRecord.from_row(r) for r in rows]

    # -- transitions --------------------------------------------------------

    def claim(
        self,
        *,
        key: str,
        tool: str,
        digest: str,
        account: dict[str, str],
        caller: str | None,
        text_sha256: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WriteRecord:
        """Reserve ``key`` for this request, committed before any network call.

        Returns the row in ``submitting`` when the caller should send the
        request, or in ``published`` when it already went out (replay the
        stored receipt, send nothing). Raises ``idempotency_conflict`` when
        the key belongs to a different request or account, and
        ``outcome_unknown`` when an earlier attempt is in flight or ended
        ambiguously. A ``failed`` row is re-claimed: nothing reached X.
        """
        now = _now()
        user_id, handle = account.get("user_id"), account.get("username")
        meta_json = json.dumps(meta or {}, sort_keys=True)
        with self._connect() as conn, _immediate(conn):
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO writes (idempotency_key, tool, account_user_id, account_handle,"
                    " caller, request_digest, text_sha256, state, meta_json, attempts,"
                    " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (key, tool, user_id, handle, caller, digest, text_sha256, SUBMITTING,
                     meta_json, now, now),
                )  # fmt: skip
            else:
                existing = WriteRecord.from_row(row)
                if existing.request_digest != digest or existing.account_user_id != user_id:
                    raise PulsarError(
                        IDEMPOTENCY_CONFLICT,
                        "idempotency_key was already used for a different request; "
                        "use a new key for a new write",
                        detail={"idempotency_key": key, "state": existing.state},
                    )
                if existing.state == PUBLISHED:
                    return existing
                if existing.state in (SUBMITTING, UNKNOWN):
                    raise OutcomeUnknown(
                        f"an earlier attempt with this idempotency_key is {existing.state}",
                        detail={"idempotency_key": key, "state": existing.state},
                    )
                conn.execute(
                    "UPDATE writes SET state = ?, caller = ?, account_handle = ?, meta_json = ?,"
                    " error_code = NULL, error_message = NULL, retryable = NULL,"
                    " attempts = attempts + 1, updated_at = ? WHERE idempotency_key = ?",
                    (SUBMITTING, caller, handle, meta_json, now, key),
                )
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
        return WriteRecord.from_row(row)

    def publish(
        self,
        key: str,
        *,
        post_id: str | None = None,
        media_id: str | None = None,
        url: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WriteRecord:
        return self._settle(key, PUBLISHED, post_id=post_id, media_id=media_id, url=url, meta=meta)

    def fail(
        self, key: str, error: PulsarError, *, meta: dict[str, Any] | None = None
    ) -> WriteRecord:
        """Settle a claimed row from an error: ``unknown`` if ambiguous, else ``failed``."""
        state = UNKNOWN if error.code == OUTCOME_UNKNOWN else FAILED
        return self._settle(
            key,
            state,
            error_code=error.code,
            error_message=error.message,
            retryable=error.retryable,
            meta=meta,
        )

    def _settle(
        self,
        key: str,
        state: str,
        *,
        meta: dict[str, Any] | None = None,
        **columns: Any,
    ) -> WriteRecord:
        assert state in TERMINAL_STATES
        with self._connect() as conn, _immediate(conn):
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            merged = {**json.loads(row["meta_json"] or "{}"), **(meta or {})}
            values = {k: v for k, v in columns.items() if v is not None}
            if "retryable" in values:
                values["retryable"] = int(values["retryable"])
            values.update(state=state, meta_json=json.dumps(merged, sort_keys=True))
            values["updated_at"] = _now()
            assignments = ", ".join(f"{name} = ?" for name in values)
            conn.execute(
                f"UPDATE writes SET {assignments} WHERE idempotency_key = ?",
                (*values.values(), key),
            )
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
        record = WriteRecord.from_row(row)
        if self._export is not None:
            try:
                self._export(record)
            except Exception:
                # The ledger row is committed and authoritative; a failed
                # export must not turn a published post into an error the
                # caller might "fix" by retrying.
                log.exception("writes.jsonl export failed for %s", key)
        return record


@contextlib.contextmanager
def _immediate(conn: sqlite3.Connection) -> Generator[None]:
    """BEGIN IMMEDIATE ... COMMIT: take the write lock up front, so two
    processes claiming one key serialize instead of both reading "absent"."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
