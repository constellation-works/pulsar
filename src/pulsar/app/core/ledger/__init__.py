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
blocks until reconcile settles each ambiguous item with ``settle``.

A crash, a cancelled call or a timeout therefore leaves a row behind instead
of nothing, and a second call with the same key cannot post twice: it gets
the stored receipt, an ``idempotency_conflict``, or ``outcome_unknown``.

The file is SQLite in WAL mode, created 0600 before SQLite opens it, and
opened per operation with a busy timeout so several pulsar processes can
share one home. ``PRAGMA user_version`` carries the schema version and
``schema.MIGRATIONS`` brings an older file up to date in place.

The ledger stores hashes and ids only: never post text, media bytes or
credentials, and never what a read returned.

``writes.jsonl`` is an export: every terminal transition appends one line
there through ``WriteLog.export``. The ledger is the source of truth.

``SqliteLedger(paths, read_only=True)`` serves reports: it never creates,
migrates or locks anything, and refuses a file older than this pulsar with
the remedy (``pulsar migrate --confirm``). Every connection re-reads the schema
version, so a running process stops writing once a newer pulsar migrates
the file.

Layout: ``records`` (``State`` and row values), ``keys`` (idempotency keys
and digests), ``text`` (the redaction hook for free text), ``schema`` (DDL,
migrations, transactions), ``connection`` (read-write and read-only opens),
``queries`` (reads), ``reads`` (paid provider reads), ``single`` (the legacy
single-request API), ``plans`` (the plan API), ``imports`` (historic rows),
``interfaces`` (``Ledger``, the protocol callers type against) and ``facade``
(``SqliteLedger``, its implementation).
Import from this package, not from the modules.
"""

from __future__ import annotations

from .facade import SqliteLedger
from .interfaces import Ledger
from .keys import check_key, check_note, default_key, request_digest
from .reads import ReadKind
from .records import (
    COMMITTED_ITEM_STATES,
    FAILED,
    IMPORT_TOOL,
    OPEN_ROW_STATES,
    PARTIAL,
    PENDING,
    PUBLISHED,
    RESOLVED_ABSENT,
    SKIPPED,
    SUBMITTING,
    UNKNOWN,
    AccountRef,
    ItemIntent,
    ItemRecord,
    PlanRecord,
    State,
    WriteRecord,
    derive_state,
    is_ambiguous,
    is_settled,
    parse_ts,
)
from .schema import SCHEMA_V1, SCHEMA_VERSION, is_busy
from .text import REDACTED_COLUMNS, redact_strings
from .usage import Usage

__all__ = [
    # facade
    "SqliteLedger",
    # interfaces
    "Ledger",
    # keys
    "check_key",
    "check_note",
    "default_key",
    "request_digest",
    # reads
    "ReadKind",
    # records
    "COMMITTED_ITEM_STATES",
    "FAILED",
    "IMPORT_TOOL",
    "OPEN_ROW_STATES",
    "PARTIAL",
    "PENDING",
    "PUBLISHED",
    "RESOLVED_ABSENT",
    "SKIPPED",
    "SUBMITTING",
    "UNKNOWN",
    "AccountRef",
    "derive_state",
    "is_ambiguous",
    "is_settled",
    "ItemIntent",
    "ItemRecord",
    "parse_ts",
    "PlanRecord",
    "State",
    "WriteRecord",
    # schema
    "SCHEMA_V1",
    "SCHEMA_VERSION",
    "is_busy",
    # text
    "REDACTED_COLUMNS",
    "redact_strings",
    # usage
    "Usage",
]
