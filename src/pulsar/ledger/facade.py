"""``SqliteLedger``, the ``Ledger`` over SQLite: one connection per operation,
one transaction per state change.

The SQL lives in the sibling modules; this class owns the connection, the
schema-version check, the clock and the ``writes.jsonl`` export.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
from collections.abc import Callable, Generator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from pulsar.errors import INTERNAL, INVALID_ARGUMENT, OUTCOME_UNKNOWN, PulsarError
from pulsar.home import Paths

from . import connection, imports, plans, queries, single
from .keys import check_note
from .records import AccountRef, ItemIntent, PlanRecord, State, WriteRecord, iso
from .schema import (
    BUSY_TIMEOUT_MS,
    SCHEMA_VERSION,
    immediate,
    migrate,
    needs_migration,
    newer_than_supported,
    user_version,
)
from .usage import Usage

log = logging.getLogger(__name__)

type Export = Callable[[WriteRecord | PlanRecord], Any]


def _now() -> str:
    return iso(datetime.now(UTC))


class SqliteLedger:
    """The ledger at ``paths.ledger_db``.

    ``read_only=True`` is for reports: it creates no
    directory or file, never migrates or switches the journal mode, takes
    no write lock, and works in a read-only home. A missing file reads as
    empty; a file older than this pulsar is refused with the remedy
    (``pulsar migrate``, which is ``migrate`` on a read-write ledger). Every
    state change on it raises ``internal``.
    """

    def __init__(
        self,
        paths: Paths,
        *,
        export: Export | None = None,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
        clock: Callable[[], datetime] | None = None,
        read_only: bool = False,
    ) -> None:
        self.paths = paths
        self.read_only = read_only
        self._export = export
        self._busy_timeout_ms = busy_timeout_ms
        self._clock = clock
        self._ready = False

    def reader(self) -> SqliteLedger:
        """A read-only view of the same file: for checks that must not create,
        migrate or write it (a dry run, a report)."""
        if self.read_only:
            return self
        return SqliteLedger(
            self.paths, busy_timeout_ms=self._busy_timeout_ms, clock=self._clock, read_only=True
        )

    def _stamp(self) -> str:
        return iso(self._clock()) if self._clock is not None else _now()

    # -- connection ---------------------------------------------------------

    @contextlib.contextmanager
    def _connect(self) -> Generator[sqlite3.Connection]:
        """A short-lived read-write connection in autocommit mode; callers BEGIN.

        Opening per operation keeps transactions short and lets several
        processes share the file; the busy timeout makes them queue rather
        than fail. The schema version is read on every connection, not once
        per process: a long-running server must stop the moment a newer
        pulsar migrates the file under it. Migrations
        run on the first connection, and again only if the file is older.
        """
        path = self.paths.ledger_db
        conn = connection.open_writable(self.paths, self._busy_timeout_ms)
        try:
            version = user_version(conn)
            if version > SCHEMA_VERSION:
                raise newer_than_supported(path, version)
            if not self._ready or version < SCHEMA_VERSION:
                migrate(conn, path, self._busy_timeout_ms)
                self._ready = True
            yield conn
        finally:
            conn.close()

    def _read[T](self, body: Callable[[sqlite3.Connection], T], empty: T) -> T:
        """``body`` over a connection at ``SCHEMA_VERSION``; ``empty`` when a
        read-only ledger has no file (or a file no schema was ever applied to)."""
        if not self.read_only:
            with self._connect() as conn:
                return body(conn)
        path = self.paths.ledger_db

        def checked(conn: sqlite3.Connection) -> T:
            version = user_version(conn)
            if version == 0:
                return empty
            if version > SCHEMA_VERSION:
                raise newer_than_supported(path, version)
            if version < SCHEMA_VERSION:
                raise needs_migration(path, version)
            return body(conn)

        return connection.read_only(path, self._busy_timeout_ms, checked, missing=empty)

    def _write[T](self, operation: str, body: Callable[[sqlite3.Connection], T]) -> T:
        """``body`` in one ``BEGIN IMMEDIATE`` transaction, the version re-read
        under the write lock so a concurrent upgrade cannot slip in between."""
        self._refuse_read_only(operation)
        with self._connect() as conn, immediate(conn):
            version = user_version(conn)
            if version > SCHEMA_VERSION:
                raise newer_than_supported(self.paths.ledger_db, version)
            return body(conn)

    def _refuse_read_only(self, operation: str) -> None:
        if self.read_only:
            raise PulsarError(
                INTERNAL,
                f"ledger {self.paths.ledger_db} was opened read-only; {operation} writes to it",
                detail={"path": str(self.paths.ledger_db), "operation": operation},
            )

    def migrate(self) -> tuple[int, int]:
        """Bring the file to this pulsar's schema, creating it if needed:
        ``(from_version, to_version)``. Refuses a newer file."""
        self._refuse_read_only("migrate")
        conn = connection.open_writable(self.paths, self._busy_timeout_ms)
        try:
            versions = migrate(conn, self.paths.ledger_db, self._busy_timeout_ms)
        finally:
            conn.close()
        self._ready = True
        return versions

    def schema_versions(self) -> tuple[int, int]:
        """``(the file's version, this pulsar's)`` without changing anything:
        0 for a missing file or one no schema was applied to. What ``migrate``
        would do, for a command that reports before it acts."""
        current = connection.read_only(
            self.paths.ledger_db, self._busy_timeout_ms, user_version, missing=0
        )
        return current, SCHEMA_VERSION

    # -- reads --------------------------------------------------------------

    def get(self, key: str) -> WriteRecord | None:
        return self._read(lambda conn: queries.get_write(conn, key), None)

    def get_plan(self, key: str) -> PlanRecord | None:
        return self._read(lambda conn: queries.load(conn, key), None)

    def known_post_ids(self, post_ids: Sequence[str]) -> set[str]:
        """Which of ``post_ids`` the ledger already records; reconcile must not reuse them."""
        wanted = [p for p in post_ids if p]
        if not wanted:
            return set()
        return self._read(lambda conn: queries.known_post_ids(conn, wanted), set[str]())

    def history(self, *, limit: int = 20, account_alias: str | None = None) -> list[PlanRecord]:
        """The newest rows first (by ``created_at``), optionally for one account."""
        return self._read(
            lambda conn: queries.history(conn, limit=limit, account_alias=account_alias), []
        )

    def count(self, *, account_alias: str | None = None) -> int:
        """How many rows ``history`` would match without a limit (its ``total``)."""
        return self._read(lambda conn: queries.count(conn, account_alias=account_alias), 0)

    def last_published(self, alias: str) -> PlanRecord | None:
        """``alias``'s newest ``published`` row, however many newer rows are not."""
        return self._read(lambda conn: queries.last_published(conn, alias), None)

    def usage(self, account_alias: str, *, day_start: datetime, month_start: datetime) -> Usage:
        """Money and posts committed since the window starts (see ``ledger/usage.py``)."""
        return self._read(
            lambda conn: queries.usage(
                conn, account_alias, day_start=day_start, month_start=month_start
            ),
            Usage(spent_day_usd=0.0, spent_month_usd=0.0, posts_day=0),
        )

    def unresolved(self, *, stale_after: timedelta, now: datetime) -> list[PlanRecord]:
        """What reconcile works on: ``unknown`` rows, and ``submitting`` rows whose
        newest item was submitted more than ``stale_after`` before ``now`` (a
        sender that crashed or was killed mid-thread)."""
        cutoff = iso(now - stale_after)
        return self._read(lambda conn: queries.unresolved(conn, cutoff=cutoff), [])

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
        stale_after: timedelta | None = None,
    ) -> WriteRecord:
        """Reserve ``key`` for this request, committed before any network call.

        Returns the row in ``submitting`` when the caller should send the
        request, or in ``published`` when it already went out (replay the
        stored receipt, send nothing). Raises ``idempotency_conflict`` when
        the key belongs to a different request or account, and
        ``outcome_unknown`` when an earlier attempt is in flight or ended
        ambiguously. A ``failed`` row is re-claimed: nothing reached X.

        With ``stale_after``, a ``delete_post`` or ``upload_media`` row still
        ``submitting`` that long after its last update is taken over (its
        sender died; neither request has a duplicate effect): it is returned
        ``submitting`` with ``attempts`` bumped and a ``note`` saying so.
        ``create_post`` rows are never taken over.
        """
        now = self._stamp()
        stale_before = None
        if stale_after is not None:
            if stale_after <= timedelta(0):
                raise PulsarError(
                    INVALID_ARGUMENT, f"stale_after must be positive, not {stale_after}"
                )
            stale_before = iso(datetime.fromisoformat(now) - stale_after)
        return self._write(
            "claim",
            lambda conn: single.claim(
                conn,
                now,
                key=key,
                tool=tool,
                digest=digest,
                account=account,
                caller=caller,
                text_sha256=text_sha256,
                meta=meta,
                stale_before=stale_before,
            ),
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
        return self._settle(
            key, State.PUBLISHED, post_id=post_id, media_id=media_id, url=url, meta=meta
        )

    def fail(
        self, key: str, error: PulsarError, *, meta: dict[str, Any] | None = None
    ) -> WriteRecord:
        """Settle a claimed row from an error: ``unknown`` if ambiguous, else ``failed``."""
        state = State.UNKNOWN if error.code == OUTCOME_UNKNOWN else State.FAILED
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
        state: State,
        *,
        meta: dict[str, Any] | None = None,
        **columns: Any,
    ) -> WriteRecord:
        now = self._stamp()
        record = self._write(
            "settle",
            lambda conn: single.settle(conn, now, key, state, meta=meta, columns=columns),
        )
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
            raise PulsarError(
                INVALID_ARGUMENT,
                f"plan {key!r} has no posts; a plan has at least one",
                detail={"idempotency_key": key},
            )
        _check_provider(provider, account)
        for window_start in (day_start, month_start):
            iso(window_start)  # a naive window is refused before anything is written
        now = self._stamp()
        result = self._write(
            "claim_plan",
            lambda conn: plans.claim_plan(
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
            ),
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
        self._write("begin_item", lambda conn: plans.begin_item(conn, now, key, idx))
        return now

    def item_sending(self, key: str, idx: int, stamp: str) -> str | None:
        """Re-stamp ``submitted_at`` just before the post request leaves (after
        media uploads, which can take minutes), so staleness is measured from
        the send. A compare-and-set on ``stamp``: ``None`` means the item is no
        longer this sender's (reconcile settled it, or a retry took it over)
        and the post must not be sent."""
        now = self._stamp()
        mine = self._write(
            "item_sending", lambda conn: plans.item_sending(conn, now, key, idx, stamp)
        )
        return now if mine else None

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
            State.PUBLISHED,
            allowed_from=(State.SUBMITTING, State.UNKNOWN),
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
            State.FAILED,
            allowed_from=(State.PENDING, State.SUBMITTING),
            error_code=error.code,
            error_message=error.message,
            retryable=int(error.retryable),
        )

    def item_unknown(self, key: str, idx: int, error: PulsarError) -> None:
        self._set_item(
            key,
            idx,
            State.UNKNOWN,
            allowed_from=(State.PENDING, State.SUBMITTING, State.UNKNOWN),
            error_code=error.code,
            error_message=error.message,
            retryable=int(error.retryable),
        )

    def _set_item(
        self, key: str, idx: int, state: State, *, allowed_from: tuple[State, ...], **columns: Any
    ) -> None:
        now = self._stamp()
        self._write(
            f"moving an item to {state}",
            lambda conn: plans.set_item(
                conn, now, key, idx, state, allowed_from=allowed_from, columns=columns
            ),
        )

    def finish(self, key: str) -> PlanRecord:
        """Settle the row from its items and export it to writes.jsonl.

        all published -> published; any unknown or still submitting -> unknown
        (a submitting item has no recorded outcome, so it becomes unknown too);
        some published and the rest failed or pending -> partial; none
        published -> failed. The row carries item 0's post id and url.
        """
        now = self._stamp()
        record = self._write("finish", lambda conn: plans.finish(conn, now, key))
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
        record = self._write(
            "settle", lambda conn: plans.settle(conn, now, key, seen=seen, verdicts=verdicts)
        )
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
        _check_provider(provider, account)
        now = self._stamp()
        changed, record = self._write(
            "skip",
            lambda conn: plans.skip(
                conn, now, key=key, provider=provider, account=account, caller=caller, note=note
            ),
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
        return self._write(
            "record_import",
            lambda conn: imports.record_import(
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
            ),
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


def _check_provider(provider: str, account: AccountRef) -> None:
    if provider != account.provider:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"provider {provider!r} is not account {account.alias!r}'s ({account.provider!r})",
            detail={"provider": provider, "account": account.alias},
        )
