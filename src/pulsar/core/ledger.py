"""The write ledger: one SQLite row per logical write, committed before it leaves.

Every post costs money and X has no idempotency key of its own, so pulsar
keeps one. A write is *claimed* — its row committed — before any request goes
out, and *settled* afterwards. The single-request API (``claim`` / ``publish``
/ ``fail``, used by the legacy tools) has three outcomes:

    submitting ──> published   X confirmed; later calls with the key replay it
               ├─> failed      X or the network proved nothing happened; retry allowed
               └─> unknown     the request may have reached X; never re-sent

The plan API (``claim_plan`` ... ``finish``) records a thread: one ``writes``
row per plan and account, and one ``items`` row per post of the thread::

    row:  pending ──> submitting ──> published | partial | failed | unknown
          skipped     (a recorded decision never to publish the key)
    item: pending ──> submitting ──> published | failed | unknown

``begin_item`` moves one item ``pending -> submitting`` as a compare-and-set
before its request leaves, so two callers holding the same pending row can
never both send it. ``finish`` derives the row state from its items. A
``partial`` thread (some posts published, the rest definitively not) resumes
after its last published post when it is claimed again; an ``unknown`` one
blocks until reconcile settles each ambiguous item with ``resolve_item``.

A crash, a cancelled call or a timeout therefore leaves a row behind instead
of nothing, and a second call with the same key cannot post twice: it gets
the stored receipt, an ``idempotency_conflict``, or ``outcome_unknown``.

The file is SQLite in WAL mode, created 0600 before SQLite opens it, and
opened per operation with a busy timeout so several pulsar processes can
share one home. ``PRAGMA user_version`` carries the schema version and
``_MIGRATIONS`` brings an older file up to date in place.

The ledger stores hashes and ids only: never post text, media bytes or
credentials.

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
from collections.abc import Callable, Generator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

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
from .jsonx import as_list, obj
from .paths import Paths
from .usage import Usage

log = logging.getLogger(__name__)

PENDING = "pending"
SUBMITTING = "submitting"
PUBLISHED = "published"
PARTIAL = "partial"
FAILED = "failed"
UNKNOWN = "unknown"
SKIPPED = "skipped"
# Outcomes of the single-request API (``claim`` / ``publish`` / ``fail``).
TERMINAL_STATES = frozenset({PUBLISHED, FAILED, UNKNOWN})
ROW_STATES = frozenset({PENDING, SUBMITTING, PUBLISHED, PARTIAL, FAILED, UNKNOWN, SKIPPED})
ITEM_STATES = frozenset({PENDING, SUBMITTING, PUBLISHED, FAILED, UNKNOWN})
# Row states ``finish`` can land on; each appends a writes.jsonl line.
FINISHED_STATES = frozenset({PUBLISHED, PARTIAL, FAILED, UNKNOWN})
# Item states that may have cost money: counted by ``usage``.
COMMITTED_ITEM_STATES = (SUBMITTING, PUBLISHED, UNKNOWN)

# Stored on an item that reconcile proved was never published. Ledger-only:
# no tool returns it as an error code.
RESOLVED_ABSENT = "outcome_resolved_absent"

IMPORT_TOOL = "import:posted.jsonl"
SKIP_TOOL = "skip"

# The single-request API predates providers and accounts: it was X only, and
# its account is the bound X user. Rows it writes are recorded as provider
# "x" with the alias ``x:<handle>``, and a ``create_post`` row mirrors itself
# as one item so ``usage`` counts it.
LEGACY_PROVIDER = "x"
LEGACY_ITEM_TOOLS = frozenset({"create_post"})

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

# v2: providers, account aliases, plans and threads. SQLite cannot alter a
# CHECK constraint, so ``writes`` is rebuilt (create, copy, drop, rename) to
# widen its states; ``items`` holds one row per post of a thread. Existing
# ``create_post`` rows are back-filled with the one item they describe.
_SCHEMA_V2 = """
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
_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1, 2: _SCHEMA_V2}
SCHEMA_VERSION = max(_MIGRATIONS)


def _iso(when: datetime) -> str:
    """The one timestamp format the ledger stores, so strings compare as times."""
    if when.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return when.astimezone(UTC).isoformat(timespec="milliseconds")


def _now() -> str:
    return _iso(datetime.now(UTC))


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


def check_note(note: str | None, *, what: str = "note") -> str | None:
    """A free-text note is stored verbatim; refuse one that looks like a credential."""
    if note is not None and scan_for_secrets(note):
        raise PulsarError(SECRET_DETECTED, f"{what} looks like a credential; it is stored verbatim")
    return note


def _conflict(key: str, state: str) -> PulsarError:
    return PulsarError(
        IDEMPOTENCY_CONFLICT,
        "idempotency_key was already used for a different request; use a new key for a new write",
        detail={"idempotency_key": key, "state": state},
    )


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
            meta=obj(json.loads(row["meta_json"] or "{}")),
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# -- the plan API's values --------------------------------------------------------


@dataclass(frozen=True)
class AccountRef:
    """The account a plan row publishes as. ``alias`` is ``provider:handle``."""

    alias: str
    provider: str
    user_id: str | None = None
    handle: str | None = None


@dataclass(frozen=True)
class ItemIntent:
    """One post of a thread, as claimed: hashes and the estimated price only."""

    text_sha256: str
    fingerprint: str
    est_cost_usd: float


@dataclass(frozen=True)
class ItemRecord:
    idx: int
    state: str
    text_sha256: str | None = None
    fingerprint: str | None = None
    est_cost_usd: float = 0.0
    post_id: str | None = None
    url: str | None = None
    media_ids: tuple[str, ...] = ()
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    submitted_at: str | None = None
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> ItemRecord:
        return cls(
            idx=row["idx"],
            state=row["state"],
            text_sha256=row["text_sha256"],
            fingerprint=row["fingerprint"],
            est_cost_usd=float(row["est_cost_usd"]),
            post_id=row["post_id"],
            url=row["url"],
            media_ids=tuple(str(m) for m in as_list(json.loads(row["media_ids_json"] or "[]"))),
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=None if row["retryable"] is None else bool(row["retryable"]),
            submitted_at=row["submitted_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "idx": self.idx,
            "state": self.state,
            "post_id": self.post_id,
            "url": self.url,
            "text_sha256": self.text_sha256,
            "fingerprint": self.fingerprint,
            "est_cost_usd": self.est_cost_usd,
            "media_ids": list(self.media_ids),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "retryable": self.retryable,
            "submitted_at": self.submitted_at,
        }


@dataclass(frozen=True)
class PlanRecord:
    """A ``writes`` row with its items. ``digest`` is the plan digest (None for
    rows the plan API did not claim: legacy tools, imports, skips)."""

    key: str
    tool: str
    state: str
    request_digest: str
    provider: str | None = None
    account_alias: str | None = None
    account_user_id: str | None = None
    account_handle: str | None = None
    digest: str | None = None
    caller: str | None = None
    note: str | None = None
    post_id: str | None = None
    url: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 1
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    items: tuple[ItemRecord, ...] = ()

    @classmethod
    def from_rows(cls, row: sqlite3.Row, items: Sequence[sqlite3.Row]) -> PlanRecord:
        return cls(
            key=row["idempotency_key"],
            tool=row["tool"],
            state=row["state"],
            request_digest=row["request_digest"],
            provider=row["provider"],
            account_alias=row["account_alias"],
            account_user_id=row["account_user_id"],
            account_handle=row["account_handle"],
            digest=row["plan_digest"],
            caller=row["caller"],
            note=row["note"],
            post_id=row["post_id"],
            url=row["url"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            meta=obj(json.loads(row["meta_json"] or "{}")),
            items=tuple(ItemRecord.from_row(i) for i in items),
        )

    @property
    def resume_from(self) -> int | None:
        """Index of the first item not yet published (None when all are)."""
        return next((i.idx for i in self.items if i.state != PUBLISHED), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.key,
            "tool": self.tool,
            "state": self.state,
            "provider": self.provider,
            "account_alias": self.account_alias,
            "account_user_id": self.account_user_id,
            "account_handle": self.account_handle,
            "digest": self.digest,
            "caller": self.caller,
            "note": self.note,
            "post_id": self.post_id,
            "url": self.url,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "meta": dict(self.meta),
            "items": [i.to_dict() for i in self.items],
        }


def derive_state(items: Sequence[ItemRecord]) -> str:
    """A plan row's state from its items' (see the module docstring)."""
    states = [i.state for i in items]
    if states and all(s == PUBLISHED for s in states):
        return PUBLISHED
    if any(s in (UNKNOWN, SUBMITTING) for s in states):
        return UNKNOWN
    if any(s == PUBLISHED for s in states):
        return PARTIAL
    return FAILED


type Export = Callable[[WriteRecord | PlanRecord], Any]


class Ledger:
    def __init__(
        self,
        paths: Paths,
        *,
        export: Export | None = None,
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

    def get_plan(self, key: str) -> PlanRecord | None:
        with self._connect() as conn:
            return _load(conn, key)

    def history(self, *, limit: int = 20, account_alias: str | None = None) -> list[PlanRecord]:
        """The newest rows first (by ``created_at``), optionally for one account."""
        with self._connect() as conn:
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
            return [_with_items(conn, r) for r in rows]

    def usage(self, account_alias: str, *, day_start: datetime, month_start: datetime) -> Usage:
        """Money and posts committed since the window starts (see ``core/usage.py``)."""
        with self._connect() as conn:
            return _usage(conn, account_alias, day_start=day_start, month_start=month_start)

    def unresolved(self, *, stale_after: timedelta, now: datetime) -> list[PlanRecord]:
        """What reconcile works on: ``unknown`` rows, and ``submitting`` rows whose
        newest item was submitted more than ``stale_after`` before ``now`` (a
        sender that crashed or was killed mid-thread)."""
        cutoff = _iso(now - stale_after)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT w.* FROM writes w WHERE w.state = ?"
                " OR (w.state = ? AND (SELECT MAX(i.submitted_at) FROM items i"
                "     WHERE i.write_id = w.id) < ?)"
                " ORDER BY w.id",
                (UNKNOWN, SUBMITTING, cutoff),
            ).fetchall()
            return [_with_items(conn, r) for r in rows]

    # -- single-request transitions (legacy tools) ----------------------------

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
        alias = f"{LEGACY_PROVIDER}:{handle.lower()}" if handle else None
        meta_json = json.dumps(meta or {}, sort_keys=True)
        with self._connect() as conn, _immediate(conn):
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
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
                    raise _conflict(key, existing.state)
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
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
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
            merged = {**obj(json.loads(row["meta_json"] or "{}")), **(meta or {})}
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
            row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
        record = WriteRecord.from_row(row)
        self._emit(record)
        return record

    # -- plan transitions -------------------------------------------------------

    def claim_plan(
        self,
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
    ) -> PlanRecord:
        """Reserve ``key`` for a plan on one account, in one ``BEGIN IMMEDIATE``.

        Returns the row ``pending`` (send it: ``begin_item`` each post from
        ``resume_from``), ``published`` (replay the stored receipt) or
        ``skipped`` (a recorded decision never to publish; send nothing).
        ``admit`` is the policy check: it gets the committed ``Usage`` and
        raises to refuse, in which case nothing is written. It runs inside
        the transaction, so it must not touch the ledger itself.

        Raises ``idempotency_conflict`` when the key belongs to another plan,
        tool or account, and ``outcome_unknown`` when an earlier attempt is
        in flight (``submitting``) or ended ambiguously (``unknown``). A
        ``failed`` or ``partial`` row is re-armed: its failed items go back
        to ``pending`` and its published ones stay published.
        """
        if not items:
            raise ValueError("a plan has at least one item")
        if provider != account.provider:
            raise ValueError(f"provider {provider!r} is not the account's {account.provider!r}")
        now = _now()
        with self._connect() as conn, _immediate(conn):
            existing = _load(conn, key)
            if existing is None:
                if admit is not None:
                    admit(_usage(conn, account.alias, day_start=day_start, month_start=month_start))
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
                return _require(conn, key)
            if existing.account_alias != account.alias:
                raise _conflict(key, existing.state)
            if existing.state == SKIPPED:
                return existing
            # An imported row carries no plan digest (the routine that wrote it
            # kept no request), so the key and account alone identify it.
            if existing.tool != IMPORT_TOOL and (
                existing.tool != tool or existing.request_digest != digest
            ):
                raise _conflict(key, existing.state)
            if existing.state == PUBLISHED:
                return existing
            in_flight = any(i.state == SUBMITTING for i in existing.items)
            if existing.state in (SUBMITTING, UNKNOWN) or in_flight:
                state = SUBMITTING if in_flight else existing.state
                raise OutcomeUnknown(
                    f"an earlier attempt with this idempotency_key is {state}",
                    detail={"idempotency_key": key, "state": state},
                )
            if admit is not None:
                admit(_usage(conn, account.alias, day_start=day_start, month_start=month_start))
            write_id = _write_id(conn, key)
            if existing.state == PENDING:
                conn.execute(
                    "UPDATE writes SET caller = ?, updated_at = ? WHERE id = ?",
                    (caller, now, write_id),
                )
            else:  # failed or partial: nothing is in flight; re-send what did not go out
                conn.execute(
                    "UPDATE items SET state = ?, error_code = NULL, error_message = NULL,"
                    " retryable = NULL, submitted_at = NULL, updated_at = ?"
                    " WHERE write_id = ? AND state = ?",
                    (PENDING, now, write_id, FAILED),
                )
                conn.execute(
                    "UPDATE writes SET state = ?, caller = ?, error_code = NULL,"
                    " error_message = NULL, retryable = NULL, attempts = attempts + 1,"
                    " updated_at = ? WHERE id = ?",
                    (PENDING, caller, now, write_id),
                )
            return _require(conn, key)

    def begin_item(self, key: str, idx: int) -> None:
        """Compare-and-set item ``idx`` from ``pending`` to ``submitting``, committed
        before its request leaves. Raises ``outcome_unknown`` when the item is
        not pending (another caller started it) or the row is no longer open."""
        now = _now()
        with self._connect() as conn, _immediate(conn):
            record = _require(conn, key)
            item = _item(record, idx)
            if record.state not in (PENDING, SUBMITTING) or item.state != PENDING:
                state = record.state if record.state != PENDING else item.state
                raise OutcomeUnknown(
                    f"post {idx} of this idempotency_key is {item.state} (row {record.state}); "
                    "another call may be sending it",
                    detail={"idempotency_key": key, "state": state, "idx": idx},
                )
            if any(i.state != PUBLISHED for i in record.items[:idx]):
                raise PulsarError(
                    INVALID_ARGUMENT,
                    f"post {idx} cannot start before the posts ahead of it are published",
                    detail={"idempotency_key": key, "idx": idx},
                )
            write_id = _write_id(conn, key)
            cur = conn.execute(
                "UPDATE items SET state = ?, submitted_at = ?, updated_at = ?"
                " WHERE write_id = ? AND idx = ? AND state = ?",
                (SUBMITTING, now, now, write_id, idx, PENDING),
            )
            assert cur.rowcount == 1  # the IMMEDIATE lock makes the check above the CAS
            conn.execute(
                "UPDATE writes SET state = ?, updated_at = ? WHERE id = ?",
                (SUBMITTING, now, write_id),
            )

    def item_published(
        self,
        key: str,
        idx: int,
        *,
        post_id: str,
        url: str,
        media_ids: Sequence[str] = (),
    ) -> None:
        self._set_item(
            key,
            idx,
            PUBLISHED,
            allowed_from=(SUBMITTING, UNKNOWN),
            post_id=post_id,
            url=url,
            media_ids_json=json.dumps(list(media_ids)),
            error_code=None,
            error_message=None,
            retryable=None,
        )

    def item_failed(self, key: str, idx: int, error: PulsarError) -> None:
        """Nothing was published for this item. An ``outcome_unknown`` error is
        recorded as ``unknown`` instead: an ambiguous send is never definitive."""
        if error.code == OUTCOME_UNKNOWN:
            self.item_unknown(key, idx, error)
            return
        self._set_item(
            key,
            idx,
            FAILED,
            allowed_from=(PENDING, SUBMITTING),
            error_code=error.code,
            error_message=error.message,
            retryable=int(error.retryable),
        )

    def item_unknown(self, key: str, idx: int, error: PulsarError) -> None:
        self._set_item(
            key,
            idx,
            UNKNOWN,
            allowed_from=(PENDING, SUBMITTING, UNKNOWN),
            error_code=error.code,
            error_message=error.message,
            retryable=int(error.retryable),
        )

    def resolve_item(self, key: str, idx: int, *, post_id: str | None, url: str | None) -> None:
        """Reconcile's verdict on an ``unknown`` or ``submitting`` item: it was
        published as ``post_id``, or (``post_id`` None) provably never was.
        Call ``finish`` afterwards."""
        if post_id is not None:
            self._set_item(
                key,
                idx,
                PUBLISHED,
                allowed_from=(SUBMITTING, UNKNOWN),
                post_id=post_id,
                url=url,
                error_code=None,
                error_message=None,
                retryable=None,
            )
        else:
            self._set_item(
                key,
                idx,
                FAILED,
                allowed_from=(SUBMITTING, UNKNOWN),
                error_code=RESOLVED_ABSENT,
                error_message="reconcile found no such post on the account; it was not published",
                retryable=1,
            )

    def _set_item(
        self, key: str, idx: int, state: str, *, allowed_from: tuple[str, ...], **columns: Any
    ) -> None:
        now = _now()
        with self._connect() as conn, _immediate(conn):
            record = _require(conn, key)
            item = _item(record, idx)
            if item.state not in allowed_from:
                raise ValueError(
                    f"{key!r} post {idx} is {item.state}; cannot move it to {state}"
                    f" (only from {', '.join(allowed_from)})"
                )
            values = {**columns, "state": state, "updated_at": now}
            assignments = ", ".join(f"{name} = ?" for name in values)
            conn.execute(
                f"UPDATE items SET {assignments} WHERE write_id = ? AND idx = ?",
                (*values.values(), _write_id(conn, key), idx),
            )

    def finish(self, key: str) -> PlanRecord:
        """Settle the row from its items and export it to writes.jsonl.

        all published -> published; any unknown or still submitting -> unknown
        (a submitting item has no recorded outcome, so it becomes unknown too);
        some published and the rest failed or pending -> partial; none
        published -> failed. The row carries item 0's post id and url.
        """
        now = _now()
        with self._connect() as conn, _immediate(conn):
            record = _require(conn, key)
            if record.state == SKIPPED or not record.items:
                raise ValueError(f"{key!r} is {record.state} with no posts; nothing to finish")
            write_id = _write_id(conn, key)
            conn.execute(
                "UPDATE items SET state = ?, error_code = ?, error_message = ?, retryable = 0,"
                " updated_at = ? WHERE write_id = ? AND state = ?",
                (UNKNOWN, OUTCOME_UNKNOWN, "no outcome was recorded for this post", now,
                 write_id, SUBMITTING),
            )  # fmt: skip
            record = _require(conn, key)
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
                    write_id,
                ),
            )
            record = _require(conn, key)
        self._emit(record)
        return record

    def skip(
        self,
        *,
        key: str,
        provider: str,
        account: AccountRef,
        caller: str | None,
        note: str | None,
    ) -> PlanRecord:
        """Record a decision never to publish ``key`` on ``account``.

        A later ``claim_plan`` with the key returns the row ``skipped`` and
        sends nothing. Skipping a key that was published, is in flight or
        ended ambiguously is ``idempotency_conflict``; skipping it twice is a
        no-op. A pending, failed or partial row becomes skipped (its
        published posts, if any, stay recorded).
        """
        check_note(note)
        if provider != account.provider:
            raise ValueError(f"provider {provider!r} is not the account's {account.provider!r}")
        now = _now()
        with self._connect() as conn, _immediate(conn):
            existing = _load(conn, key)
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
                    raise _conflict(key, existing.state)
                if existing.state == SKIPPED:
                    return existing
                if existing.state not in (PENDING, FAILED, PARTIAL) or any(
                    i.state == SUBMITTING for i in existing.items
                ):
                    raise _conflict(key, existing.state)
                conn.execute(
                    "UPDATE writes SET state = ?, note = ?, caller = ?, updated_at = ?"
                    " WHERE idempotency_key = ?",
                    (SKIPPED, note, caller, now, key),
                )
            record = _require(conn, key)
        self._emit(record)
        return record

    def record_import(
        self,
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
        """Insert a settled historic row unless ``key`` exists: ``(inserted, row)``.

        ``post_id`` makes it ``published`` with one published item (costing
        nothing: historic spend is not re-counted); without one it is
        ``skipped``. Nothing is exported: these rows were not written by pulsar.
        """
        check_note(note)
        when = _iso(created_at)
        now = _now()
        state = SKIPPED if post_id is None else PUBLISHED
        with self._connect() as conn, _immediate(conn):
            existing = _load(conn, key)
            if existing is not None:
                return False, existing
            cur = conn.execute(
                "INSERT INTO writes (idempotency_key, tool, provider, account_alias,"
                " account_user_id, account_handle, caller, request_digest, text_sha256, state,"
                " post_id, url, note, meta_json, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (key, tool, provider, account.alias, account.user_id, account.handle, caller,
                 digest, text_sha256, state, post_id, url, note,
                 json.dumps(meta, sort_keys=True), when, now),
            )  # fmt: skip
            if post_id is not None:
                conn.execute(
                    "INSERT INTO items (write_id, idx, state, text_sha256, est_cost_usd,"
                    " post_id, url, submitted_at, updated_at) VALUES (?, 0, ?, ?, 0, ?, ?, ?, ?)",
                    (cur.lastrowid, PUBLISHED, text_sha256, post_id, url, when, now),
                )
            return True, _require(conn, key)

    def _emit(self, record: WriteRecord | PlanRecord) -> None:
        if self._export is None:
            return
        try:
            self._export(record)
        except Exception:
            # The ledger row is committed and authoritative; a failed
            # export must not turn a published post into an error the
            # caller might "fix" by retrying.
            key = record.idempotency_key if isinstance(record, WriteRecord) else record.key
            log.exception("writes.jsonl export failed for %s", key)


# -- helpers over an open connection ---------------------------------------------


def _with_items(conn: sqlite3.Connection, row: sqlite3.Row) -> PlanRecord:
    items = conn.execute("SELECT * FROM items WHERE write_id = ? ORDER BY idx", (row["id"],))
    return PlanRecord.from_rows(row, items.fetchall())


def _load(conn: sqlite3.Connection, key: str) -> PlanRecord | None:
    row = conn.execute("SELECT * FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
    return None if row is None else _with_items(conn, row)


def _require(conn: sqlite3.Connection, key: str) -> PlanRecord:
    record = _load(conn, key)
    if record is None:
        raise KeyError(key)
    return record


def _write_id(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT id FROM writes WHERE idempotency_key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(key)
    return int(row["id"])


def _item(record: PlanRecord, idx: int) -> ItemRecord:
    for item in record.items:
        if item.idx == idx:
            return item
    raise ValueError(f"{record.key!r} has no post {idx} (it has {len(record.items)})")


def _usage(
    conn: sqlite3.Connection, account_alias: str, *, day_start: datetime, month_start: datetime
) -> Usage:
    """Committed items (submitting, published, unknown) by their ``submitted_at``:
    spend over every account, posts for ``account_alias``."""
    day, month = _iso(day_start), _iso(month_start)
    placeholders = ", ".join("?" for _ in COMMITTED_ITEM_STATES)
    row = conn.execute(
        "SELECT"
        " COALESCE(SUM(CASE WHEN i.submitted_at >= ? THEN i.est_cost_usd END), 0),"
        " COALESCE(SUM(CASE WHEN i.submitted_at >= ? THEN i.est_cost_usd END), 0),"
        " COUNT(CASE WHEN i.submitted_at >= ? AND w.account_alias = ? THEN 1 END)"
        " FROM items i JOIN writes w ON w.id = i.write_id"
        f" WHERE i.state IN ({placeholders}) AND i.submitted_at >= ?",
        (day, month, day, account_alias, *COMMITTED_ITEM_STATES, min(day, month)),
    ).fetchone()
    return Usage(
        spent_day_usd=round(float(row[0]), 6),
        spent_month_usd=round(float(row[1]), 6),
        posts_day=int(row[2]),
    )


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
