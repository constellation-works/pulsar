"""Filesystem layout of the pulsar host state.

Everything pulsar persists lives under one directory (``PULSAR_HOME``,
default ``~/.config/pulsar``). Several accounts share it::

    key                          one Fernet key for every account (0600)
    client.json                  OAuth app client ids, one per provider
    accounts.json                the account registry (see accounts.py)
    accounts.lock                serialises registry read-modify-writes
    accounts/<slug>/tokens.enc   that account's encrypted token bundle
    accounts/<slug>/refresh.lock that account's cross-process refresh lock

``tokens.enc``, ``refresh.lock`` and ``whoami.json`` at the root are the
phase 1 single-account layout, kept only so it can be migrated. Nothing in
here is a secret by itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import INVALID_ARGUMENT, PulsarError
from .fsutil import ensure_private_dir

_PROVIDER = re.compile(r"[a-z0-9]{1,32}")
_HANDLE = re.compile(r"[a-z0-9_-][a-z0-9._-]{0,99}")  # no leading dot
SLUG_SEPARATOR = "--"


def account_slug(alias: str) -> str:
    """The directory name for a canonical ``provider:handle`` alias.

    ``provider--handle``, with the provider limited to ``[a-z0-9]`` (so it
    never contains the separator) and the handle to ``[a-z0-9._-]`` without a
    leading dot, so the mapping is injective and reversible and no alias can
    name a path outside ``accounts/`` (no ``/``; the ``provider--`` prefix
    already rules out ``.`` and ``..``). Anything else is ``invalid_argument``.
    """
    provider, sep, handle = alias.partition(":")
    if not sep or not _PROVIDER.fullmatch(provider) or not _HANDLE.fullmatch(handle):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"account {alias!r} is not a storable alias: the provider must be [a-z0-9] and "
            "the handle [a-z0-9._-] with no leading dot (at most 100 characters)",
            detail={"account": alias},
        )
    return f"{provider}{SLUG_SEPARATOR}{handle}"


def alias_from_slug(slug: str) -> str | None:
    """Invert ``account_slug``; None for a name it could not have produced."""
    provider, sep, handle = slug.partition(SLUG_SEPARATOR)
    alias = f"{provider}:{handle}"
    if not sep:
        return None
    try:
        return alias if account_slug(alias) == slug else None
    except PulsarError:
        return None


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

    @property
    def accounts_file(self) -> Path:
        return self.home / "accounts.json"

    @property
    def accounts_lock(self) -> Path:
        return self.home / "accounts.lock"

    @property
    def accounts_dir(self) -> Path:
        return self.home / "accounts"

    def account_dir(self, alias: str) -> Path:
        return self.accounts_dir / account_slug(alias)

    def ensure(self) -> None:
        ensure_private_dir(self.home)


def default_paths() -> Paths:
    return Paths(pulsar_home())
