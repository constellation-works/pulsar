"""Import the retired x-updates routine's ``posted.jsonl`` into the ledger.

That routine kept one JSON object per line::

    {"key": "pr:example:5", "ts": "2026-09-13T18:43:21+00:00",
     "post_id": "1000000000000000002", "text": "...",
     "superseded_post_id": "...", "superseded_note": "..."}   # optional
    {"key": "pr:example:4", "ts": "...", "post_id": null, "note": "skipped — ..."}

A line with a ``post_id`` becomes a ``published`` row with one published
item; ``post_id: null`` becomes a ``skipped`` row carrying the line's note.
The key is kept as the idempotency key, so a routine now publishing through
pulsar with the same keys replays the imported receipt, or gets ``skipped``,
instead of posting again. Only the text's SHA-256 is stored, never the text.
Historic spend is not re-counted (every item costs 0).

The import is idempotent: a line whose row is already there is counted as
``already_present``. A key taken by a write that was not imported, or an
imported row that disagrees with its line, is reported as a conflict and left
alone. A malformed line is reported with its line number and skipped; it never
stops the rest.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import PulsarError
from .jsonx import JSONObject, as_object
from .ledger import (
    IMPORT_TOOL,
    PUBLISHED,
    SKIPPED,
    AccountRef,
    Ledger,
    PlanRecord,
    check_key,
    check_note,
    request_digest,
)
from .writelog import text_sha256

IMPORT_CALLER = "pulsar-import"
_META_KEYS = ("superseded_post_id", "superseded_note")


@dataclass
class ImportReport:
    imported_published: int = 0
    imported_skipped: int = 0
    already_present: int = 0
    conflicts: list[tuple[int, str, str]] = field(default_factory=list)  # (line, key, reason)
    errors: list[tuple[int, str]] = field(default_factory=list)  # (line, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "imported_published": self.imported_published,
            "imported_skipped": self.imported_skipped,
            "already_present": self.already_present,
            "conflicts": [
                {"line": line, "key": key, "reason": reason} for line, key, reason in self.conflicts
            ],
            "errors": [{"line": line, "reason": reason} for line, reason in self.errors],
        }


@dataclass(frozen=True)
class _Line:
    key: str
    ts: datetime
    post_id: str | None
    text_sha256: str | None
    note: str | None
    meta: dict[str, Any]


class _BadLine(ValueError):
    pass


def _optional_str(data: JSONObject, name: str) -> str | None:
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _BadLine(f"`{name}` must be a string or null")
    return value


def _parse(raw: bytes) -> _Line:
    try:
        decoded: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _BadLine(f"not a JSON line: {exc}") from exc
    data = as_object(decoded)
    if data is None:
        raise _BadLine("not a JSON object")
    try:
        key = check_key(data.get("key"))
    except PulsarError as exc:
        raise _BadLine(f"bad `key`: {exc.message}") from exc
    if key is None:
        raise _BadLine("missing `key`")
    raw_ts = data.get("ts")
    if not isinstance(raw_ts, str):
        raise _BadLine("missing `ts`")
    try:
        ts = datetime.fromisoformat(raw_ts.strip())
    except ValueError as exc:
        raise _BadLine(f"`ts` is not ISO 8601: {raw_ts!r}") from exc
    if ts.tzinfo is None:
        raise _BadLine(f"`ts` has no timezone: {raw_ts!r}")
    post_id = _optional_str(data, "post_id")
    if post_id is not None and not post_id.strip():
        raise _BadLine("`post_id` is empty")
    text = _optional_str(data, "text")
    meta: dict[str, Any] = {}
    try:
        note = check_note(_optional_str(data, "note"))
        for name in _META_KEYS:
            value = check_note(_optional_str(data, name), what=name)
            if value is not None:
                meta[name] = value
    except PulsarError as exc:
        raise _BadLine(exc.message) from exc
    return _Line(
        key=key,
        ts=ts,
        post_id=post_id.strip() if post_id is not None else None,
        text_sha256=text_sha256(text) if text is not None else None,
        note=note,
        meta=meta,
    )


def _disagreements(row: PlanRecord, line: _Line, digest: str, account: AccountRef) -> list[str]:
    first = row.items[0] if row.items else None
    expected: dict[str, tuple[object, object]] = {
        "state": (row.state, SKIPPED if line.post_id is None else PUBLISHED),
        "post_id": (first.post_id if first else None, line.post_id),
        "request_digest": (row.request_digest, digest),
        "account_alias": (row.account_alias, account.alias),
        "text_sha256": (first.text_sha256 if first else None, line.text_sha256),
        "note": (row.note, line.note),
        "meta": (row.meta, line.meta),
    }
    return [name for name, (have, want) in expected.items() if have != want]


def import_posted(
    ledger: Ledger,
    path: Path | str,
    *,
    account: AccountRef,
    provider: str = "x",
    url_for: Callable[[str], str],
) -> ImportReport:
    """Import ``path`` (a ``posted.jsonl``) as rows for ``account``.

    ``url_for`` builds a post's public URL from its id (provider-specific,
    so the caller supplies it).
    """
    report = ImportReport()
    with Path(path).open("rb") as fh:
        for number, raw in enumerate(fh, start=1):
            if not raw.strip():
                continue
            try:
                line = _parse(raw)
            except _BadLine as exc:
                report.errors.append((number, str(exc)))
                continue
            digest = request_digest(IMPORT_TOOL, key=line.key, post_id=line.post_id)
            inserted, row = ledger.record_import(
                key=line.key,
                tool=IMPORT_TOOL,
                digest=digest,
                provider=provider,
                account=account,
                caller=IMPORT_CALLER,
                created_at=line.ts,
                post_id=line.post_id,
                url=url_for(line.post_id) if line.post_id is not None else None,
                text_sha256=line.text_sha256,
                note=line.note,
                meta=line.meta,
            )
            if inserted:
                if line.post_id is None:
                    report.imported_skipped += 1
                else:
                    report.imported_published += 1
            elif row.tool != IMPORT_TOOL:
                report.conflicts.append(
                    (number, line.key, f"key already used by a {row.tool} write ({row.state})")
                )
            elif differ := _disagreements(row, line, digest, account):
                report.conflicts.append(
                    (number, line.key, "imported row disagrees on " + ", ".join(differ))
                )
            else:
                report.already_present += 1
    return report
