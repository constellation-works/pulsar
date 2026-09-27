"""Structured errors: ``PulsarError`` carries a ``code`` from ``codes``.

``retryable`` says whether repeating the *same* call later can succeed
without the caller changing anything. It is ``False`` for
``outcome_unknown`` on purpose: the write may already be live, and a blind
retry of a non-idempotent post publishes (and pays for) it twice.
"""

from __future__ import annotations

from typing import Any

from .codes import AUTH_EXPIRED, OUTCOME_UNKNOWN, RETRYABLE_CODES


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
        # One shape whatever the error: ``detail`` is null, never absent.
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "detail": self.detail,
        }


class AuthExpired(PulsarError):
    def __init__(
        self,
        message: str = "authorization expired; a human must re-run "
        "`pulsar auth login --account <alias>`",
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
