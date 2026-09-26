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

Core never reads the environment or ``$HOME`` itself (STD-02 §R3): a surface
resolves them once (``Paths.from_environ``) and hands the result down. The
home itself must not be a symlink (STD-05 §R9): pulsar resolves nothing, so
an operator whose state lives elsewhere points ``PULSAR_HOME`` at the real
directory.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .errors import INVALID_ARGUMENT, PulsarError
from .fsutil import ensure_private_dir, require_private

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


def expand_user(raw: str, user_home: Path | None) -> Path | None:
    """``raw`` with a leading ``~`` or ``~/`` expanded against ``user_home``.

    None when ``raw`` needs a home and none was given. ``~name`` is not
    expanded (it stays a relative path, which callers refuse).
    """
    if raw != "~" and not raw.startswith("~/"):
        return Path(raw)
    if user_home is None:
        return None
    return user_home / raw[2:]


def resolve_home(environ: Mapping[str, str], user_home: Path) -> Path:
    """The pulsar home: ``PULSAR_HOME`` from ``environ``, else ``<user_home>/.config/pulsar``."""
    override = environ.get("PULSAR_HOME")
    if override:
        return expand_user(override, user_home) or Path(override)
    return user_home / ".config" / "pulsar"


@dataclass(frozen=True)
class Paths:
    home: Path
    # The operator's home directory, resolved by the surface: ``~`` in
    # config.toml expands against it and media roots may not contain it.
    # None when the surface gave none; then ``~`` in config is refused.
    user_home: Path | None = None

    @classmethod
    def from_environ(cls, environ: Mapping[str, str], user_home: Path) -> Paths:
        """The layout a surface resolved from its environment and the user's home."""
        return cls(resolve_home(environ, user_home), user_home=user_home)

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

    def _home_symlink_fix(self) -> str:
        real = shlex.quote(os.path.realpath(self.home))
        return f"point PULSAR_HOME at the real directory: PULSAR_HOME={real}"

    def check_home(self) -> None:
        """``insecure_storage`` if the home is a symlink, not the user's, or wider than 0700."""
        require_private(self.home, is_dir=True, symlink_fix=self._home_symlink_fix())

    def ensure(self) -> None:
        """Create the home 0700 if missing; refuse an unsafe existing one (``check_home``)."""
        ensure_private_dir(self.home, symlink_fix=self._home_symlink_fix())


def default_paths() -> Paths:
    """The surface-side convenience: the layout for this process's environment and ``$HOME``.

    Only surfaces call this; core receives the resulting ``Paths``. New code
    resolves once with ``Paths.from_environ(os.environ, Path.home())``.
    """
    return Paths.from_environ(os.environ, Path.home())


def pulsar_home() -> Path:
    """``default_paths().home``; kept for surfaces that have not switched to ``Paths``."""
    return default_paths().home
