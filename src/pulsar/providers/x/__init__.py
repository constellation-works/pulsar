"""X (Twitter): OAuth 2.0 PKCE, the v2 API client, capabilities.

This module is the X provider's public API: the layers above import from
``pulsar.providers.x`` only, and only what ``__all__`` lists. Modules inside
the package import each other directly. ``tests/test_layering.py`` enforces it.
"""

from __future__ import annotations

from .adapter import XChannel, post_url
from .auth import load_client_id, login
from .client import REFRESH_AHEAD_SECONDS, MediaProcessingError, XClient, check_x_id
from .text import validate_text

__all__ = [
    # adapter
    "XChannel",
    "post_url",
    # auth
    "load_client_id",
    "login",
    # client
    "REFRESH_AHEAD_SECONDS",
    "MediaProcessingError",
    "XClient",
    "check_x_id",
    # text
    "validate_text",
]
