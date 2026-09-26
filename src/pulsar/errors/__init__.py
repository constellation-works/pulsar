"""Pulsar's failure vocabulary: the stable error codes every surface reports
(``codes``) and the exceptions that carry them (``exceptions``).
"""

from __future__ import annotations

from .codes import (
    ACCOUNT_MISMATCH,
    API_ERROR,
    AUTH_EXPIRED,
    BUDGET_EXCEEDED,
    CREDENTIALS_UNREADABLE,
    DAILY_CAP,
    DUPLICATE,
    FORBIDDEN,
    IDEMPOTENCY_CONFLICT,
    INSECURE_STORAGE,
    INTERNAL,
    INVALID_ARGUMENT,
    INVALID_CONFIG,
    INVALID_MEDIA,
    INVALID_PLAN,
    INVALID_TEXT,
    LOCK_TIMEOUT,
    NOT_DUE,
    NOT_FOUND,
    OUTCOME_UNKNOWN,
    QUIET_HOURS,
    RATE_LIMITED,
    SECRET_DETECTED,
    UNKNOWN_ACCOUNT,
    UNSUPPORTED,
)
from .exceptions import AuthExpired, OutcomeUnknown, PulsarError

__all__ = [
    # codes
    "ACCOUNT_MISMATCH",
    "API_ERROR",
    "AUTH_EXPIRED",
    "BUDGET_EXCEEDED",
    "CREDENTIALS_UNREADABLE",
    "DAILY_CAP",
    "DUPLICATE",
    "FORBIDDEN",
    "IDEMPOTENCY_CONFLICT",
    "INSECURE_STORAGE",
    "INTERNAL",
    "INVALID_ARGUMENT",
    "INVALID_CONFIG",
    "INVALID_MEDIA",
    "INVALID_PLAN",
    "INVALID_TEXT",
    "LOCK_TIMEOUT",
    "NOT_DUE",
    "NOT_FOUND",
    "OUTCOME_UNKNOWN",
    "QUIET_HOURS",
    "RATE_LIMITED",
    "SECRET_DETECTED",
    "UNKNOWN_ACCOUNT",
    "UNSUPPORTED",
    # exceptions
    "AuthExpired",
    "OutcomeUnknown",
    "PulsarError",
]
