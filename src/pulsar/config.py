"""Filesystem layout and constants for the pulsar host state.

Everything pulsar persists lives under one directory (``PULSAR_HOME``,
default ``~/.config/pulsar``). The token bundle is encrypted at rest; the
key file next to it is created mode 0600. Nothing in here is a secret by
itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

X_AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
X_TOKEN_URL = "https://api.x.com/2/oauth2/token"
X_API_BASE = "https://api.x.com/2"
SCOPES = ("tweet.read", "tweet.write", "users.read", "offline.access")

CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8976
CALLBACK_PATH = "/callback"

# Rough X credit pricing; verify live before relying on it (see README).
COST_PLAIN_POST_USD = 0.015
COST_URL_POST_USD = 0.20

MAX_POST_WEIGHTED_LENGTH = 280
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})


def pulsar_home() -> Path:
    override = os.environ.get("PULSAR_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "pulsar"


@dataclass(frozen=True)
class Paths:
    home: Path

    @property
    def key_file(self) -> Path:
        return self.home / "key"

    @property
    def token_file(self) -> Path:
        return self.home / "tokens.enc"

    @property
    def client_file(self) -> Path:
        return self.home / "client.json"

    @property
    def whoami_cache(self) -> Path:
        return self.home / "whoami.json"

    @property
    def write_log(self) -> Path:
        return self.home / "writes.jsonl"

    def ensure(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)


def default_paths() -> Paths:
    return Paths(pulsar_home())


def callback_url() -> str:
    return f"http://{CALLBACK_HOST}:{CALLBACK_PORT}{CALLBACK_PATH}"
