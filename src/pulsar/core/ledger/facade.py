"""``Ledger``: one connection per operation, one transaction per state change.

The SQL lives in the sibling modules; this class owns the connection, the
clock and the ``writes.jsonl`` export.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Generator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from ..errors import OUTCOME_UNKNOWN, PulsarError
from ..fsutil import append_private
from ..paths import Paths
from ..usage import Usage
from . import imports, plans, queries, single
from .keys import check_note
from .records import (
    FAILED,
    PENDING,
    PUBLISHED,
    RESOLVED_ABSENT,
    SUBMITTING,
    UNKNOWN,
    AccountRef,
    ItemIntent,
    PlanRecord,
    WriteRecord,
    iso,
)
from .schema import BUSY_TIMEOUT_MS, immediate, migrate

log = logging.getLogger(__name__)

type Export = Callable[[WriteRecord | PlanRecord], Any]


def _now() -> str:
    return iso(datetime.now(UTC))


class Ledger:
    def __init__(
        self,
        paths: Paths,
        *,
        export: Export | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self._export = export
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._ready = False

    def _stamp(self) -> str:
        return iso(self._clock()) if self._clock is not None else _now()

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
                migrate(conn, self._busy_timeout_ms)
                self._ready = True
            yield conn
        finally:
            conn.close()

    # -- reads --------------------------------------------------------------

    def get(self, key: str) -> WriteRecord | None:
        with self._connect() as conn:
            return queries.get_write(conn, key)

    def all(self) -> list[WriteRecord]:
        with self._connect() as conn:
            return queries.all_writes(conn)

    def get_plan(self, key: str) -> PlanRecord | None:
        with self._connect() as conn:
            return queries.load(conn, key)

    def known_post_ids(self, post_ids: Sequence[str]) -> set[str]:
        """Which of ``post_ids`` the ledger already records; reconcile must not reuse them."""
        wanted = [p for p in post_ids if p]
        if not wanted:
            return set()
        with self._connect() as conn:
            return queries.known_post_ids(conn, wanted)

    def history(self, *, limit: int = 20, account_alias: str | None = None) -> list[PlanRecord]:
        """The newest rows first (by ``created_at``), optionally for one account."""
        with self._connect() as conn:
            return queries.history(conn, limit=limit, account_alias=account_alias)

    def usage(self, account_alias: str, *, day_start: datetime, month_start: datetime) -> Usage:
        """Money and posts committed since the window starts (see ``core/usage.py``)."""
        with self._connect() as conn:
            return queries.usage(conn, account_alias, day_start=day_start, month_start=month_start)

    def unresolved(self, *, stale_after: timedelta, now: datetime) -> list[PlanRecord]:
        """What reconcile works on: ``unknown`` rows, and ``submitting`` rows whose
        newest item was submitted more than ``stale_after`` before ``now`` (a
        sender that crashed or was killed mid-thread)."""
        cutoff = iso(now - stale_after)
        with self._connect() as conn:
            return queries.unresolved(conn, cutoff=cutoff)

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
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            return single.claim(
                conn,
                now,
                key=key,
                tool=tool,
                digest=digest,
                account=account,
                caller=caller,
                text_sha256=text_sha256,
                meta=meta,
            )

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
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            record = single.settle(conn, now, key, state, meta=meta, columns=columns)
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
        to ``pending`` and its published ones stay published. Re-armed and
        still-pending items take the new intent's fingerprint and price.

        Pending items of an open row count toward ``usage`` from the claim
        on (a reservation), so ``admit`` for a second plan sees what a
        thread in progress was admitted with.
        """
        if not items:
            raise ValueError("a plan has at least one item")
        if provider != account.provider:
            raise ValueError(f"provider {provider!r} is not the account's {account.provider!r}")
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            result = plans.claim_plan(
                conn,
                now,
                key=key,
                tool=tool,
                digest=digest,
                provider=provider,
                account=account,
                caller=caller,
                items=items,
                admit=admit,
                day_start=day_start,
                month_start=month_start,
            )
        if isinstance(result, PulsarError):
            raise result
        return result

    def begin_item(self, key: str, idx: int) -> str:
        """Compare-and-set item ``idx`` from ``pending`` to ``submitting``, committed
        before its request leaves. Returns the ``submitted_at`` stamp, the
        sender's token for ``item_sending``. Raises ``outcome_unknown`` when
        the item is not pending (another caller started it) or the row is no
        longer open."""
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            plans.begin_item(conn, now, key, idx)
        return now

    def item_sending(self, key: str, idx: int, stamp: str) -> str | None:
        """Re-stamp ``submitted_at`` just before the post request leaves (after
        media uploads, which can take minutes), so staleness is measured from
        the send. A compare-and-set on ``stamp``: ``None`` means the item is no
        longer this sender's (reconcile settled it, or a retry took it over)
        and the post must not be sent."""
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            return now if plans.item_sending(conn, now, key, idx, stamp) else None

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
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            plans.set_item(conn, now, key, idx, state, allowed_from=allowed_from, columns=columns)

    def finish(self, key: str) -> PlanRecord:
        """Settle the row from its items and export it to writes.jsonl.

        all published -> published; any unknown or still submitting -> unknown
        (a submitting item has no recorded outcome, so it becomes unknown too);
        some published and the rest failed or pending -> partial; none
        published -> failed. The row carries item 0's post id and url.
        """
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            record = plans.finish(conn, now, key)
        self._emit(record)
        return record

    def settle(
        self,
        key: str,
        *,
        seen: Mapping[int, tuple[str, str | None]],
        verdicts: Mapping[int, tuple[str, str | None] | None],
    ) -> PlanRecord | None:
        """Reconcile's verdicts and ``finish``, in one transaction, if the row is
        as reconcile saw it.

        ``seen`` is each open (unknown or submitting) item's ``(state,
        submitted_at)`` when reconcile listed the row; ``verdicts`` maps an
        item to ``(post_id, url)`` (published) or ``None`` (provably absent).
        If the row's open items changed since (a live sender re-stamped or
        finished one, or started another), nothing is written and ``None`` is
        returned: the row is not reconcile's to settle.
        """
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            record = plans.settle(conn, now, key, seen=seen, verdicts=verdicts)
        if record is not None:
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
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            changed, record = plans.skip(
                conn, now, key=key, provider=provider, account=account, caller=caller, note=note
            )
        if changed:
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
        now = self._stamp()
        with self._connect() as conn, immediate(conn):
            return imports.record_import(
                conn,
                now,
                key=key,
                tool=tool,
                digest=digest,
                provider=provider,
                account=account,
                caller=caller,
                created_at=created_at,
                post_id=post_id,
                url=url,
                text_sha256=text_sha256,
                note=note,
                meta=meta,
            )

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
