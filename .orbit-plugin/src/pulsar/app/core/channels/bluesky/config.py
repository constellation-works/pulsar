"""Bluesky (AT Protocol) endpoints, record names and post limits."""

from __future__ import annotations

PROVIDER = "bsky"
# The PDS a client talks to unless it is handed the account's own.
BSKY_SERVICE = "https://bsky.social"
# Where bsky.social's authorization server refreshes an OAuth token.
BSKY_TOKEN_URL = "https://bsky.social/oauth/token"
APP_URL = "https://bsky.app"
POST_COLLECTION = "app.bsky.feed.post"
# app.bsky.feed.post#text: maxGraphemes 300, maxLength 3000 (UTF-8 bytes).
MAX_POST_GRAPHEMES = 300
MAX_POST_BYTES = 3000
