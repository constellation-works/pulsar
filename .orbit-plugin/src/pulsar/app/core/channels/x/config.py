"""X endpoints, OAuth scopes, the loopback callback and post limits."""

from __future__ import annotations

from ..loopback import HOST as CALLBACK_HOST
from ..loopback import PATH as CALLBACK_PATH
from ..loopback import PORT as CALLBACK_PORT

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API_BASE = "https://api.x.com/2"
SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")
MAX_POST_WEIGHTED_LENGTH = 280


def callback_url() -> str:
    return f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
