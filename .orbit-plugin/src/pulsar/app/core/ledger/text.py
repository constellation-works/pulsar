"""Free text on its way into the ledger: the one redaction hook.

Every column that can hold free text (an error message from a provider, a
note, the self-asserted caller label, string values in ``meta_json``) is
written through ``persisted_text``, and ``REDACTED_COLUMNS`` is the inventory
a test holds every writer to. Keys, digests, ids, states and timestamps are
structured and are not in it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast

from pulsar.internal.fs import as_object
from pulsar.internal.guard import redact

# ``table.column`` for every column written through ``persisted_text``.
REDACTED_COLUMNS = (
    "writes.caller",
    "writes.error_message",
    "writes.note",
    "writes.meta_json",
    "items.error_message",
    "reads.caller",
    "approvals.approved_by",
    "approvals.source",
)


def persisted_text(value: str | None) -> str | None:
    """``value`` as the ledger may store it.

    Callers look this up on the module at call time (``text.persisted_text``)
    so the one hook covers every writer.
    """
    return None if value is None else redact(value)


def persisted_meta(meta: Mapping[str, Any]) -> str:
    """``meta_json`` for ``meta``, every string in it through ``persisted_text``."""
    return json.dumps(_redact_strings(dict(meta)), sort_keys=True)


def redact_strings(value: object) -> object:
    """``value`` with every string in it, at any depth, through ``persisted_text``."""
    return _redact_strings(value)


def _redact_strings(value: object) -> object:
    if isinstance(value, str):
        return persisted_text(value)
    if (mapping := as_object(value)) is not None:
        return {k: _redact_strings(v) for k, v in mapping.items()}
    if isinstance(value, list | tuple):
        return [_redact_strings(v) for v in cast(Sequence[object], value)]
    return value
