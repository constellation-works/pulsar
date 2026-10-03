"""Bluesky (AT Protocol): the XRPC client, its DPoP hook, the OAuth login, text rules,
capabilities."""

from __future__ import annotations

from .adapter import BlueskyChannel, check_post_uri, fingerprint, parse_post_uri, post_url
from .auth import BlueskyLogin, check_client_id, loopback_client_id
from .client import REFRESH_AHEAD_SECONDS, BlueskyClient
from .config import BSKY_SERVICE, BSKY_TOKEN_URL, PROVIDER
from .dpop import Es256Proof
from .interfaces import BlueskyApi, DpopProof
from .text import Facet, detect_facets, grapheme_count, validate_text

__all__ = [
    # adapter
    "BlueskyChannel",
    "check_post_uri",
    "fingerprint",
    "parse_post_uri",
    "post_url",
    # auth
    "BlueskyLogin",
    "check_client_id",
    "loopback_client_id",
    # client
    "REFRESH_AHEAD_SECONDS",
    "BlueskyClient",
    # config
    "BSKY_SERVICE",
    "BSKY_TOKEN_URL",
    "PROVIDER",
    # dpop
    "Es256Proof",
    # interfaces
    "BlueskyApi",
    "DpopProof",
    # text
    "Facet",
    "detect_facets",
    "grapheme_count",
    "validate_text",
]
