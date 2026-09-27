"""The error codes: every tool failure carries one, machine-readable, and a
caller branches on it, never on the message."""

from __future__ import annotations

# Codes an agent may branch on. Keep this list in sync with
# docs/design/surfaces/references/error-codes.md.
AUTH_EXPIRED = "auth_expired"
INVALID_TEXT = "invalid_text"
INVALID_ARGUMENT = "invalid_argument"
SECRET_DETECTED = "secret_detected"
INVALID_MEDIA = "invalid_media"
DUPLICATE = "duplicate"
FORBIDDEN = "forbidden"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
API_ERROR = "api_error"
INVALID_CONFIG = "invalid_config"
INSECURE_STORAGE = "insecure_storage"
IDEMPOTENCY_CONFLICT = "idempotency_conflict"
OUTCOME_UNKNOWN = "outcome_unknown"
INVALID_PLAN = "invalid_plan"
ACCOUNT_MISMATCH = "account_mismatch"
UNKNOWN_ACCOUNT = "unknown_account"
UNSUPPORTED = "unsupported"
BUDGET_EXCEEDED = "budget_exceeded"
DAILY_CAP = "daily_cap"
QUIET_HOURS = "quiet_hours"
NOT_DUE = "not_due"
# A lock another pulsar process held past the deadline; ``detail.holder`` names it.
LOCK_TIMEOUT = "lock_timeout"
# A stored credential exists but cannot be read (wrong key, corrupt or newer bundle).
CREDENTIALS_UNREADABLE = "credentials_unreadable"
# A bug or an unexpected failure inside pulsar, never the provider's answer.
INTERNAL = "internal"

# Codes where repeating the identical call later can succeed. The policy
# codes are retryable because the window moves: the day or month rolls over,
# quiet hours end (``detail.retry_after`` says when).
RETRYABLE_CODES = frozenset(
    {RATE_LIMITED, API_ERROR, BUDGET_EXCEEDED, DAILY_CAP, QUIET_HOURS, NOT_DUE, LOCK_TIMEOUT}
)
