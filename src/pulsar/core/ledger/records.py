"""The ledger's values: states, row and item records, and timestamps."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..jsonx import as_list, obj

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
# Rows whose ``pending`` items are still going to be sent: those items are
# reserved against the budget from the claim on, so a concurrent claim cannot
# spend what a thread in progress was admitted with.
OPEN_ROW_STATES = (PENDING, SUBMITTING)

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


def iso(when: datetime) -> str:
    """The one timestamp format the ledger stores, so strings compare as times."""
    if when.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return when.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_ts(value: str) -> datetime:
    """A stored ledger timestamp as an aware UTC datetime."""
    return datetime.fromisoformat(value).astimezone(UTC)


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
    """A plan row's state from its items' (see the package docstring)."""
    states = [i.state for i in items]
    if states and all(s == PUBLISHED for s in states):
        return PUBLISHED
    if any(s in (UNKNOWN, SUBMITTING) for s in states):
        return UNKNOWN
    if any(s == PUBLISHED for s in states):
        return PARTIAL
    return FAILED
