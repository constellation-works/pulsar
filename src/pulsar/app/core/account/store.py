"""Where an account's token bundle lives, and what that storage does and does not protect.

``CredentialStore`` (``channels.credentials``) is the interface the rest of
pulsar codes against. ``FernetFileStore`` is today's implementation: a Fernet ciphertext
(``tokens.enc``) beside a host-local key file (``key``), both 0600 in a 0700
pulsar home. Orbit's host-held secrets (ORB-13009) are meant to drop in
behind the same protocol, which is why ``save`` already takes an
``expected_previous`` bundle for compare-and-swap rotation.

The bundle is the tokens and, for a DPoP-bound (Bluesky) login, the private
key they are bound to: one ciphertext, so the key is kept exactly like the
tokens.

What the file store protects against: the bundle showing up in plaintext in
a backup, a ``cat`` or ``grep`` over the home directory, a stray ``git add``,
or being read by *another* local user. Loading refuses a home directory
wider than 0700, or a key or bundle wider than 0600, owned by someone else,
or reached through a symlink (``insecure_storage``), rather than quietly
using a credential others could have copied or swapped.

A bundle that is there but cannot be read — the key was replaced, the file
is corrupt, or a newer pulsar wrote fields this one does not know — is
``credentials_unreadable``, never "not logged in": that would send the
operator to re-login, which overwrites the bundle instead of recovering it.

What it does not protect against: any process running as the same uid can
read the key and the ciphertext and decrypt them — encryption here is not a
boundary between processes of one user. On the Mac that includes an
Orbit-sandboxed worker that can read ``~/.config``. The long-term fix is to
move the refresh token out of the user's files entirely (ORB-13008,
ORB-13009); until then the boundary is the one the spec asks for: the
*agent* never sees raw secrets, the connector process does.

Refresh coordination also lives here: X rotates the refresh token on every
use, so two processes sharing one home must never refresh at the same time.
``refresh_lock`` is an exclusive ``flock`` on ``refresh.lock`` that the
refresher holds across load → token POST → save. ``rebind`` (login) and
``clear`` (logout) take the same lock, so a refresh in flight can never
write the previous account's rotated tokens over a new login, or back after
a logout. Waiting for it is bounded (``lock_timeout``, naming the holder).

Each login mints a ``binding_id`` that refreshes carry forward. The cached
identity (the account's row in ``accounts.json``; ``whoami.json`` in the
phase 1 layout) records the binding it describes and is ignored once that no
longer matches the stored bundle, so a lookup that raced a re-login cannot
keep naming the old account.

Several accounts share one home: ``FernetFileStore.for_account`` keeps each
account's bundle and refresh lock in ``accounts/<slug>/`` under the one
root ``key``, so accounts refresh independently. ``FernetFileStore(paths)``
is the phase 1 root store (``tokens.enc``, ``refresh.lock``), kept for the
migration.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shlex
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from pulsar.app.core.channels.credentials import (
    REFRESH_LOCK_WAIT_SECONDS,
    CredentialConflict,
    TokenBundle,
)
from pulsar.internal.errors import CREDENTIALS_UNREADABLE, INSECURE_STORAGE, PulsarError
from pulsar.internal.fs import (
    Paths,
    alias_from_slug,
    as_object,
    ensure_private_dir,
    hold_lock,
    hold_lock_async,
    publish_new_private,
    require_private,
    write_private_atomic,
)
from pulsar.internal.guard import register_live_secret

_BUNDLE_FIELDS = frozenset(f.name for f in dataclasses.fields(TokenBundle))
# Written only when set, so a bundle without them (any X bundle) stays readable
# by a pulsar from before they existed.
_DPOP_FIELDS = ("dpop_key", "service", "token_url")


def _is_str(value: object, *, optional: bool = False) -> bool:
    return isinstance(value, str) or (optional and value is None)


def _bundle_from_json(fields: dict[str, Any]) -> TokenBundle | None:
    """The bundle ``fields`` describe, or None when a field is missing or mistyped."""
    expires_at = fields.get("expires_at")
    ok = (
        _is_str(fields.get("access_token"))
        and _is_str(fields.get("refresh_token"), optional=True)
        and isinstance(expires_at, int | float)
        and not isinstance(expires_at, bool)
        and _is_str(fields.get("scope"))
        and _is_str(fields.get("client_id"))
        and _is_str(fields.get("token_type", "bearer"))
        and _is_str(fields.get("binding_id"), optional=True)
        and all(_is_str(fields.get(name), optional=True) for name in _DPOP_FIELDS)
    )
    return TokenBundle(**fields) if ok else None


def home_command(home: Path, command: str) -> str:
    """``pulsar <command>`` pinned to ``home``, so a remedy shown by the Orbit
    plugin acts on the plugin's home, not the operator's default."""
    return f"PULSAR_HOME={shlex.quote(str(home))} pulsar {command}"


def _register(bundle: TokenBundle) -> None:
    """Mask this bundle's tokens and DPoP key in every log line and stored text."""
    register_live_secret(bundle.access_token)
    register_live_secret(bundle.refresh_token)
    register_live_secret(bundle.dpop_key)


def _bundle_json(bundle: TokenBundle) -> bytes:
    fields = asdict(bundle)
    for name in _DPOP_FIELDS:
        if fields[name] is None:
            del fields[name]
    return json.dumps(fields).encode()


def login_command(home: Path, alias: str | None) -> str:
    """The command a human runs to (re)bind ``alias`` in ``home``."""
    return home_command(home, f"auth login --account {alias or 'x:<handle>'}")


class FernetFileStore:
    """``CredentialStore`` on local files: Fernet ciphertext beside its key.

    ``FernetFileStore(paths)`` is the phase 1 single-account store at the
    home root; ``for_account`` is one account's store under ``accounts/``.
    """

    def __init__(self, paths: Paths, *, account_dir: Path | None = None) -> None:
        self.paths = paths
        self.account_dir = account_dir
        if account_dir is None:
            self.alias: str | None = None
            self.token_file = paths.token_file
            self.lock_file = paths.refresh_lock
            # The phase 1 identity cache; accounts keep theirs in the registry.
            self._identity_file: Path | None = paths.whoami_cache
            # Directories below the home that hold this store's files.
            self._dirs: tuple[Path, ...] = ()
        else:
            self.alias = alias_from_slug(account_dir.name)
            self.token_file = account_dir / "tokens.enc"
            self.lock_file = account_dir / "refresh.lock"
            self._identity_file = None
            self._dirs = (account_dir.parent, account_dir)

    @classmethod
    def for_account(cls, paths: Paths, alias: str) -> FernetFileStore:
        """The store of the account ``alias`` (canonical; ``invalid_argument`` if unsafe)."""
        return cls(paths, account_dir=paths.account_dir(alias))

    def reauth_hint(self) -> str:
        return f"a human runs `{login_command(self.paths.home, self.alias)}`"

    @property
    def _label(self) -> str:
        return self.alias or "the legacy root bundle"

    # -- permissions --------------------------------------------------------

    def _check(self) -> None:
        self.paths.check_home()
        for directory in self._dirs:
            require_private(directory, is_dir=True)
        require_private(self.paths.key_file)
        require_private(self.token_file)

    def _prepare_home(self) -> None:
        """Create the home (and account dirs) 0700 if missing; refuse, never fix, unsafe ones."""
        self.paths.ensure()
        self._check()
        for directory in self._dirs:
            ensure_private_dir(directory)

    # -- key ----------------------------------------------------------------

    def _read_key(self) -> Fernet:
        try:
            raw = self.paths.key_file.read_bytes().strip()
            key = Fernet(raw)
            register_live_secret(raw.decode("ascii", "replace"))
            return key
        except ValueError as exc:
            # Never echo the key bytes; the path and the recovery are enough.
            raise PulsarError(
                INSECURE_STORAGE,
                f"{self.paths.key_file} is not a valid Fernet key (corrupt or truncated); "
                "restore it from backup, or remove it and tokens.enc and re-run "
                "`pulsar auth login`",
                detail={"path": str(self.paths.key_file)},
            ) from exc

    def _create_key(self) -> Fernet:
        """Create the key exactly once, even with several processes racing.

        ``publish_new_private`` never exposes a half-written key and fails if
        the name exists; the loser reads the winner's key, so two processes
        can never encrypt under different keys.
        """
        with contextlib.suppress(FileExistsError):
            publish_new_private(self.paths.key_file, Fernet.generate_key())
        require_private(self.paths.key_file)
        return self._read_key()

    def _fernet(self, create: bool) -> Fernet:
        try:
            return self._read_key()
        except FileNotFoundError:
            if not create:
                raise
        return self._create_key()

    # -- CredentialStore ----------------------------------------------------

    def exists(self) -> bool:
        return self.token_file.exists() and self.paths.key_file.exists()

    def _unreadable(self, problem: str, fix: str) -> PulsarError:
        return PulsarError(
            CREDENTIALS_UNREADABLE,
            f"{self.token_file} {problem}; pulsar will not overwrite it. Fix: {fix}",
            detail={"path": str(self.token_file), "key": str(self.paths.key_file)},
        )

    def load(self) -> TokenBundle | None:
        if not self.paths.home.exists():
            return None
        self._check()
        if not self.exists():
            return None
        try:
            raw = self._fernet(create=False).decrypt(self.token_file.read_bytes())
        except FileNotFoundError:
            return None  # removed (logout) between exists() and the read
        except InvalidToken:
            raise self._unreadable(
                f"cannot be decrypted with {self.paths.key_file} (the key was replaced, or "
                "the bundle is corrupt)",
                f"restore the key that encrypted it from backup. Only if that key is lost for "
                f"good: remove {shlex.quote(str(self.token_file))} and "
                f"{self.reauth_hint().removeprefix('a human runs ')}",
            ) from None
        try:
            fields = as_object(json.loads(raw))
        except ValueError:
            fields = None
        if fields is not None and set(fields) - _BUNDLE_FIELDS:
            unknown = sorted(set(fields) - _BUNDLE_FIELDS)
            raise self._unreadable(
                f"was written by a newer pulsar (unknown fields {unknown})",
                "upgrade pulsar on this host",
            )
        bundle = _bundle_from_json(fields) if fields is not None else None
        if bundle is None:
            raise self._unreadable(
                "decrypts but is not a token bundle (corrupt)",
                f"restore it from backup, or remove it and "
                f"{self.reauth_hint().removeprefix('a human runs ')}",
            )
        _register(bundle)
        return bundle

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        # The compare is atomic with the write only while the caller holds
        # refresh_lock, which every refresher does.
        self._prepare_home()
        if expected_previous is not None and self.load() != expected_previous:
            raise CredentialConflict()
        _register(bundle)
        blob = self._fernet(create=True).encrypt(_bundle_json(bundle))
        write_private_atomic(self.token_file, blob)

    def rebind(
        self, bundle: TokenBundle, *, on_bound: Callable[[TokenBundle], object] | None = None
    ) -> TokenBundle:
        bundle.binding_id = uuid.uuid4().hex
        with self.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS, purpose="login"):
            self.save(bundle)
            self._drop_identity()
            if on_bound is not None:
                on_bound(bundle)
        return bundle

    def clear(self, *, on_cleared: Callable[[], object] | None = None) -> None:
        if not self.paths.home.exists():
            return
        with self.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS, purpose="logout"):
            if self.token_file.exists():
                self.token_file.unlink()
            self._drop_identity()
            if on_cleared is not None:
                on_cleared()

    def _drop_identity(self) -> None:
        if self._identity_file is not None:
            with contextlib.suppress(FileNotFoundError):
                self._identity_file.unlink()

    # -- refresh lock -------------------------------------------------------

    def _lock_args(self, timeout: float, purpose: str) -> dict[str, Any]:
        return {
            "label": f"{purpose} of {self._label}",
            "what": f"the token refresh lock of {self._label}",
            "timeout": timeout,
        }

    @contextlib.asynccontextmanager
    async def refresh_lock(
        self, timeout: float, *, purpose: str = "token refresh"
    ) -> AsyncGenerator[None]:
        self._prepare_home()
        async with hold_lock_async(self.lock_file, **self._lock_args(timeout, purpose)):
            yield

    @contextlib.contextmanager
    def refresh_lock_sync(
        self, timeout: float, *, purpose: str = "token refresh"
    ) -> Generator[None]:
        """``refresh_lock`` for the synchronous CLI paths (login, logout, migration)."""
        self._prepare_home()
        with hold_lock(self.lock_file, **self._lock_args(timeout, purpose)):
            yield
