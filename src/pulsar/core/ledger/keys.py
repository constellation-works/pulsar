"""Idempotency keys and request digests: what makes two calls the same write."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from ..errors import IDEMPOTENCY_CONFLICT, INVALID_ARGUMENT, SECRET_DETECTED, PulsarError
from ..guard import scan_for_secrets

MAX_KEY_LENGTH = 200


def request_digest(tool: str, **fields: Any) -> str:
    """SHA-256 of the canonical JSON of what the caller asked for.

    Two calls with one idempotency key must be the same request; comparing
    digests catches a caller reusing a key for different text.
    """
    blob = json.dumps(
        {"tool": tool, **fields}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_key(digest: str, user_id: str) -> str:
    """Key for callers that pass none: the same request as the same account."""
    return hashlib.sha256(f"{digest}:{user_id}".encode()).hexdigest()


def check_key(key: object) -> str | None:
    """Validate a caller-supplied idempotency key (``None`` means derive one)."""
    if key is None:
        return None
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= MAX_KEY_LENGTH
        or any(c.isspace() or unicodedata.category(c).startswith("C") for c in key)
    ):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"idempotency_key must be 1-{MAX_KEY_LENGTH} characters with no whitespace "
            "or control characters",
        )
    if scan_for_secrets(key):
        # It is stored and exported verbatim; never let a credential in that way.
        raise PulsarError(
            SECRET_DETECTED, "idempotency_key looks like a credential; use an opaque id"
        )
    return key


def check_note(note: str | None, *, what: str = "note") -> str | None:
    """A free-text note is stored verbatim; refuse one that looks like a credential."""
    if note is not None and scan_for_secrets(note):
        raise PulsarError(SECRET_DETECTED, f"{what} looks like a credential; it is stored verbatim")
    return note


def conflict(key: str, state: str) -> PulsarError:
    return PulsarError(
        IDEMPOTENCY_CONFLICT,
        "idempotency_key was already used for a different request; use a new key for a new write",
        detail={"idempotency_key": key, "state": state},
    )
