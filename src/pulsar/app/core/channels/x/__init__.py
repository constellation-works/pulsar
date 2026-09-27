"""X (Twitter): OAuth 2.0 PKCE, the v2 API client, capabilities."""

from __future__ import annotations

from .adapter import XChannel, fingerprint, post_url, remote_fingerprint
from .auth import PROVIDER, CallbackServer
from .client import REFRESH_AHEAD_SECONDS, MediaProcessingError, XClient, bounded_text, check_x_id
from .config import callback_url
from .interfaces import XApi
from .text import validate_text, weighted_length

__all__ = [
    # adapter
    "fingerprint",
    "post_url",
    "remote_fingerprint",
    "XChannel",
    # auth
    "PROVIDER",
    "CallbackServer",
    # client
    "REFRESH_AHEAD_SECONDS",
    "bounded_text",
    "check_x_id",
    "MediaProcessingError",
    "XClient",
    # config
    "callback_url",
    # interfaces
    "XApi",
    # text
    "validate_text",
    "weighted_length",
]
