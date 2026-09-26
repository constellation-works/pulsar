"""``Ledger``: what the rest of pulsar codes against. ``SqliteLedger`` is the
implementation; the meaning of each method is documented there."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

from pulsar.errors import PulsarError

from .records import AccountRef, ItemIntent, PlanRecord, WriteRecord
from .usage import Usage


class Ledger(Protocol):
    """One row per logical write, committed before it leaves."""

    def reader(self) -> Ledger: ...

    def migrate(self) -> tuple[int, int]: ...

    def schema_versions(self) -> tuple[int, int]: ...

    def get(self, key: str) -> WriteRecord | None: ...

    def get_plan(self, key: str) -> PlanRecord | None: ...

    def known_post_ids(self, post_ids: Sequence[str]) -> set[str]: ...

    def history(self, *, limit: int = 20, account_alias: str | None = None) -> list[PlanRecord]: ...

    def count(self, *, account_alias: str | None = None) -> int: ...

    def last_published(self, alias: str) -> PlanRecord | None: ...

    def usage(self, account_alias: str, *, day_start: datetime, month_start: datetime) -> Usage: ...

    def unresolved(self, *, stale_after: timedelta, now: datetime) -> list[PlanRecord]: ...

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
    ) -> WriteRecord: ...

    def publish(
        self,
        key: str,
        *,
        post_id: str | None = None,
        media_id: str | None = None,
        url: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> WriteRecord: ...

    def fail(
        self, key: str, error: PulsarError, *, meta: dict[str, Any] | None = None
    ) -> WriteRecord: ...

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
    ) -> PlanRecord: ...

    def begin_item(self, key: str, idx: int) -> str: ...

    def item_sending(self, key: str, idx: int, stamp: str) -> str | None: ...

    def item_published(
        self, key: str, idx: int, *, post_id: str, url: str, media_ids: Sequence[str] = ()
    ) -> None: ...

    def item_failed(self, key: str, idx: int, error: PulsarError) -> None: ...

    def item_unknown(self, key: str, idx: int, error: PulsarError) -> None: ...

    def finish(self, key: str) -> PlanRecord: ...

    def settle(
        self,
        key: str,
        *,
        seen: Mapping[int, tuple[str, str | None]],
        verdicts: Mapping[int, tuple[str, str | None] | None],
    ) -> PlanRecord | None: ...

    def skip(
        self, *, key: str, provider: str, account: AccountRef, caller: str | None, note: str | None
    ) -> PlanRecord: ...

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
    ) -> tuple[bool, PlanRecord]: ...
