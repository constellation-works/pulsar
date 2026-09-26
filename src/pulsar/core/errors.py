"""Structured errors. Every tool failure carries a machine-readable ``code``.

``retryable`` says whether repeating the *same* call later can succeed
without the caller changing anything. It is ``False`` for
``outcome_unknown`` on purpose: the write may already be live, and a blind
retry of a non-idempotent post publishes (and pays for) it twice.
"""

from __future__ import annotations

from typing import Any

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
# Phase 4 (drafts and approvals); declared now so the code list is stable.
APPROVAL_REQUIRED = "approval_required"
APPROVAL_STALE = "approval_stale"

# Codes where repeating the identical call later can succeed. The policy
# codes are retryable because the window moves: the day or month rolls over,
# quiet hours end (``detail.retry_after`` says when).
RETRYABLE_CODES = frozenset(
    {RATE_LIMITED, API_ERROR, BUDGET_EXCEEDED, DAILY_CAP, QUIET_HOURS, NOT_DUE}
)


class PulsarError(Exception):
    def __init__(
        self, code: str, message: str, *, detail: Any = None, retryable: bool | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.retryable = code in RETRYABLE_CODES if retryable is None else retryable

    def to_envelope(self) -> dict[str, Any]:
        """Orbit's error envelope: ``{ok: false, error: {code, message, retryable, detail?}}``.

        The legacy MCP tools keep the flat ``to_result`` shape; new surfaces use this.
        """
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            error["detail"] = self.detail
        return {"ok": False, "error": error}

    def to_result(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class AuthExpired(PulsarError):
    def __init__(
        self, message: str = "X authorization expired; a human must re-run `pulsar auth login`"
    ) -> None:
        super().__init__(AUTH_EXPIRED, message)


class OutcomeUnknown(PulsarError):
    """The write may or may not have reached X; the caller must not retry blindly."""

    def __init__(self, cause: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(
            OUTCOME_UNKNOWN,
            f"{cause}; the write may have reached X. Do NOT retry blindly: check the "
            "account's timeline first. Repeating the call with the same idempotency_key "
            "will not post again.",
            detail={"cause": cause, **(detail or {})},
            retryable=False,
        )
        self.cause = cause
