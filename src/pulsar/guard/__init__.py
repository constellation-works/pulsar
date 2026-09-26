"""The secret scanner every outbound text passes through, and the redaction
of known secrets from what pulsar logs or stores (``scanner``).
"""

from __future__ import annotations

from .scanner import (
    SECRET_PATTERNS,
    looks_generated,
    redact,
    register_live_secret,
    scan_for_secrets,
)

__all__ = [
    # scanner
    "SECRET_PATTERNS",
    "looks_generated",
    "redact",
    "register_live_secret",
    "scan_for_secrets",
]
