"""The ledger's values: states, row and item records, and timestamps."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, assert_never

from pulsar.internal.errors import INTERNAL, INVALID_ARGUMENT, PulsarError
from pulsar.internal.fs import as_list, obj


class State(StrEnum):
    """A row's or an item's state, stored and exported as its value.

    Rows use every state; items never ``partial`` or ``skipped`` (the
    schema's CHECK constraints enforce both). Branch on a state with an
    exhaustive ``match`` ending in ``assert_never``, so a new
    state fails type checking at every decision it affects.
    """

    PENDING = "pending"
    SUBMITTING = "submitting"
    PUBLISHED = "published"
    PARTIAL = "partial"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


# The names importers used before ``State`` existed; each is the member itself.
PENDING = State.PENDING
SUBMITTING = State.SUBMITTING
PUBLISHED = State.PUBLISHED
PARTIAL = State.PARTIAL
FAILED = State.FAILED
UNKNOWN = State.UNKNOWN
SKIPPED = State.SKIPPED


def is_settled(state: State) -> bool:
    """The write's outcome is known and nothing about it is in flight.

    False for a row still being published (``pending``, ``submitting``) and
    for one only reconcile can settle (``unknown``). Reconcile's per-row
    result ``changed`` is not a ledger state (reconcile left the row alone),
    so callers reading reconcile results treat it as unsettled themselves.
    """
    match state:
        case State.PUBLISHED | State.PARTIAL | State.FAILED | State.SKIPPED:
            return True
        case State.PENDING | State.SUBMITTING | State.UNKNOWN:
            return False
        case _:
            assert_never(state)


def is_ambiguous(state: State) -> bool:
    """A request may have left with no recorded outcome: reconcile's to settle."""
    match state:
        case State.SUBMITTING | State.UNKNOWN:
            return True
        case State.PENDING | State.PUBLISHED | State.PARTIAL | State.FAILED | State.SKIPPED:
            return False
        case _:
            assert_never(state)


def is_open(state: State) -> bool:
    """A row still being published: its pending items may yet be sent."""
    match state:
        case State.PENDING | State.SUBMITTING:
            return True
        case State.PUBLISHED | State.PARTIAL | State.FAILED | State.UNKNOWN | State.SKIPPED:
            return False
        case _:
            assert_never(state)


def is_committed(state: State) -> bool:
    """An item that may have cost money: sent, or of unknown outcome."""
    match state:
        case State.SUBMITTING | State.PUBLISHED | State.UNKNOWN:
            return True
        case State.PENDING | State.FAILED:
            return False
        case State.PARTIAL | State.SKIPPED:  # row-only: no item is ever in these
            return False
        case _:
            assert_never(state)


def parse_state(value: object, *, where: str) -> State:
    """A stored state as a ``State``; anything else is a ledger invariant break."""
    try:
        return State(str(value))
    except ValueError:
        raise PulsarError(
            INTERNAL, f"ledger {where} has state {value!r}, which this pulsar does not know"
        ) from None


# Outcomes of the single-request API (``claim`` / ``publish`` / ``fail``).
TERMINAL_STATES = frozenset({PUBLISHED, FAILED, UNKNOWN})
# Item states that may have cost money: counted by ``usage``.
COMMITTED_ITEM_STATES = tuple(s for s in State if is_committed(s))
# Rows whose ``pending`` items are still going to be sent: those items are
# reserved against the budget from the claim on, so a concurrent claim cannot
# spend what a thread in progress was admitted with.
OPEN_ROW_STATES = tuple(s for s in State if is_open(s))

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
# Legacy tools whose request has no duplicate effect worth guarding: DELETE
# is idempotent at X, and a second upload only leaves an orphaned media id
# that expires. ``claim(stale_after=...)`` re-arms a ``submitting`` row of
# theirs that a crashed sender left behind, instead of blocking its key
# forever (reconcile never sees these rows: they have no items). Never
# ``create_post``: a second post is a paid duplicate.
REARMABLE_TOOLS = frozenset({"delete_post", "upload_media"})


def iso(when: datetime) -> str:
    """The one timestamp format the ledger stores, so strings compare as times."""
    if when.tzinfo is None:
        raise PulsarError(
            INVALID_ARGUMENT, f"ledger timestamps must be timezone-aware, not {when.isoformat()}"
        )
    return when.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_ts(value: str) -> datetime:
    """A stored ledger timestamp as an aware UTC datetime."""
    return datetime.fromisoformat(value).astimezone(UTC)


@dataclass(frozen=True)
class WriteRecord:
    idempotency_key: str
    tool: str
    state: State
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
            state=parse_state(row["state"], where=f"row {row['idempotency_key']!r}"),
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
    reply_to: str | None = None  # the post this one answers (a thread's first item)


@dataclass(frozen=True)
class ItemRecord:
    idx: int
    state: State
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
            state=parse_state(row["state"], where=f"item {row['write_id']}/{row['idx']}"),
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
    state: State
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
            state=parse_state(row["state"], where=f"row {row['idempotency_key']!r}"),
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


def derive_state(items: Sequence[ItemRecord]) -> State:
    """A plan row's state from its items' (see the package docstring).

    All published -> published; any ambiguous (unknown, or submitting with
    no recorded outcome) -> unknown; some published and the rest failed or
    pending -> partial; none published, or no items -> failed.
    """
    published = ambiguous = unpublished = 0
    for item in items:
        state = item.state
        match state:
            case State.PUBLISHED:
                published += 1
            case State.UNKNOWN | State.SUBMITTING:
                ambiguous += 1
            case State.PENDING | State.FAILED:
                unpublished += 1
            case State.PARTIAL | State.SKIPPED:
                raise PulsarError(
                    INTERNAL, f"post {item.idx} is {state}, a state only a row can be in"
                )
            case _:
                assert_never(state)
    if ambiguous:
        return UNKNOWN
    if published and not unpublished:
        return PUBLISHED
    if published:
        return PARTIAL
    return FAILED
