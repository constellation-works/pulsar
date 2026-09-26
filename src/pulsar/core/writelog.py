"""Append-only ``writes.jsonl``: a line-per-outcome export of the ledger.

The ledger (``ledger.py``) is the source of truth and is written *before* a
request leaves; this file gets one line each time a write reaches a terminal
state (published, failed, unknown), for tools that tail or grep JSONL.
Validation and dry runs are not writes and never appear here.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .fsutil import append_private
from .paths import Paths

if TYPE_CHECKING:
    from .ledger import WriteRecord

DEFAULT_CALLER_ENV = "PULSAR_CALLER"


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def resolve_caller(explicit: str | None) -> str:
    return explicit or os.environ.get(DEFAULT_CALLER_ENV) or "unknown"


class WriteLog:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def append(
        self,
        *,
        tool: str,
        caller: str | None,
        text: str | None = None,
        post_id: str | None = None,
        dry_run: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "tool": tool,
            "caller": resolve_caller(caller),
            "dry_run": dry_run,
            "post_id": post_id,
            "text_sha256": text_sha256(text) if text is not None else None,
        }
        if extra:
            entry.update(extra)
        self.paths.ensure()
        append_private(self.paths.write_log, json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def export(self, record: WriteRecord) -> dict[str, Any]:
        """Append one line for a ledger row that reached a terminal state.

        Keeps every field older readers expect (``ts``, ``tool``, ``caller``,
        ``dry_run``, ``post_id``, ``text_sha256``) and adds ``state`` and
        ``idempotency_key``. Only hashes and ids — never text, media bytes,
        or credentials.
        """
        extra: dict[str, Any] = {
            **record.meta,
            "state": record.state,
            "idempotency_key": record.idempotency_key,
            "text_sha256": record.text_sha256,
            "account_user_id": record.account_user_id,
        }
        if record.media_id is not None:
            extra["media_id"] = record.media_id
        if record.error_code is not None:
            extra["error_code"] = record.error_code
        return self.append(
            tool=record.tool, caller=record.caller, post_id=record.post_id, extra=extra
        )
