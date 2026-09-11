"""Structured errors. Every tool failure carries a machine-readable ``code``."""

from __future__ import annotations

from typing import Any

# Codes an agent may branch on. Keep this list in sync with the README.
AUTH_EXPIRED = "auth_expired"
INVALID_TEXT = "invalid_text"
SECRET_DETECTED = "secret_detected"
INVALID_MEDIA = "invalid_media"
DUPLICATE = "duplicate"
FORBIDDEN = "forbidden"
RATE_LIMITED = "rate_limited"
NOT_FOUND = "not_found"
API_ERROR = "api_error"


class PulsarError(Exception):
    def __init__(self, code: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def to_result(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": False, "code": self.code, "message": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class AuthExpired(PulsarError):
    def __init__(
        self, message: str = "X authorization expired; a human must re-run `pulsar auth login`"
    ) -> None:
        super().__init__(AUTH_EXPIRED, message)
