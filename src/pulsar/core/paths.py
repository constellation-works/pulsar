"""Filesystem layout of the pulsar host state.

Everything pulsar persists lives under one directory (``PULSAR_HOME``,
default ``~/.config/pulsar``). The token bundle is encrypted at rest; the
key file next to it is created mode 0600. Nothing in here is a secret by
itself.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .fsutil import ensure_private_dir


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

    @property
    def ledger_db(self) -> Path:
        return self.home / "ledger.sqlite3"

    @property
    def refresh_lock(self) -> Path:
        return self.home / "refresh.lock"

    @property
    def settings_file(self) -> Path:
        return self.home / "config.toml"

    def ensure(self) -> None:
        ensure_private_dir(self.home)


def default_paths() -> Paths:
    return Paths(pulsar_home())
