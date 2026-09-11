"""Append-only local log of every write the connector performs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any

from .config import Paths

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
        with self.paths.write_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry
