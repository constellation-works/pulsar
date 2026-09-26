"""X endpoints, OAuth scopes, the loopback callback and post limits."""

from __future__ import annotations

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API_BASE = "https://api.x.com/2"
SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8976
CALLBACK_PATH = "/callback"
MAX_POST_WEIGHTED_LENGTH = 280


def callback_url() -> str:
    return f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
