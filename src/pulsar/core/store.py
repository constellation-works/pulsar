"""Where the X token bundle lives, and what that storage does and does not protect.

``CredentialStore`` is the interface the rest of pulsar codes against.
``FernetFileStore`` is today's implementation: a Fernet ciphertext
(``tokens.enc``) beside a host-local key file (``key``), both 0600 in a 0700
pulsar home. Orbit's host-held secrets (ORB-13009) are meant to drop in
behind the same protocol, which is why ``save`` already takes an
``expected_previous`` bundle for compare-and-swap rotation.

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
import time
import uuid
from collections.abc import AsyncGenerator, Callable, Generator
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet, InvalidToken

from .errors import CREDENTIALS_UNREADABLE, INSECURE_STORAGE, INTERNAL, PulsarError
from .fsutil import (
    ensure_private_dir,
    hold_lock,
    hold_lock_async,
    publish_new_private,
    require_private,
    write_private_atomic,
)
from .guard import register_live_secret
from .jsonx import as_object, obj
from .paths import Paths, alias_from_slug

# Longer than one token POST (the HTTP timeout) so a waiter outlasts a live refresher.
REFRESH_LOCK_WAIT_SECONDS = 45.0


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    scope: str
    client_id: str
    token_type: str = "bearer"
    # Minted per login, carried across refreshes; None for bundles saved before it existed.
    binding_id: str | None = None

    def expires_within(self, seconds: float) -> bool:
        return time.time() + seconds >= self.expires_at

    @classmethod
    def from_token_response(
        cls, data: object, *, client_id: str, now: float | None = None
    ) -> TokenBundle:
        """Raises ``KeyError``/``ValueError`` on a response without a usable access token."""
        fields = obj(data)
        now = time.time() if now is None else now
        access = fields["access_token"]
        if not isinstance(access, str) or not access:
            raise ValueError("token response has no access_token")
        refresh = fields.get("refresh_token")
        # No ``expires_in`` means we do not know when the token dies. Rather
        # than invent a lifetime, treat it as expiring now: the
        # next call refreshes first, which costs one token POST at worst.
        expires_in = fields.get("expires_in")
        return cls(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=now + float(expires_in) if expires_in is not None else now,
            scope=str(fields.get("scope", "")),
            client_id=client_id,
            token_type=str(fields.get("token_type", "bearer")),
        )


_BUNDLE_FIELDS = frozenset(f.name for f in dataclasses.fields(TokenBundle))


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
    )
    return TokenBundle(**fields) if ok else None


class CredentialConflict(PulsarError):
    """A compare-and-swap save found a different bundle than the caller expected.

    Every refresher in pulsar catches this and adopts the stored bundle, so it
    reaches a caller only from a call site that forgot to; that is a bug
    (``internal``). It is still retryable: another writer saved a newer bundle,
    and a repeat call reloads the store and uses it.
    """

    def __init__(self) -> None:
        super().__init__(
            INTERNAL,
            "CredentialConflict: the stored X credential changed during refresh (a writer "
            "bypassed the refresh lock); retrying the call uses the newer bundle",
            retryable=True,
        )


def home_command(home: Path, command: str) -> str:
    """``pulsar <command>`` pinned to ``home``, so a remedy shown by the Orbit
    plugin acts on the plugin's home, not the operator's default."""
    return f"PULSAR_HOME={shlex.quote(str(home))} pulsar {command}"


def _register(bundle: TokenBundle) -> None:
    """Mask this bundle's tokens in every log line and stored text."""
    register_live_secret(bundle.access_token)
    register_live_secret(bundle.refresh_token)


def login_command(home: Path, alias: str | None) -> str:
    """The command a human runs to (re)bind ``alias`` in ``home``."""
    return home_command(home, f"auth login --account {alias or 'x:<handle>'}")


class CredentialStore(Protocol):
    """What pulsar needs from wherever the token bundle is kept."""

    def exists(self) -> bool: ...

    def reauth_hint(self) -> str:
        """What a human runs to bind this store's account again, for error messages."""
        ...

    def load(self) -> TokenBundle | None:
        """The stored bundle, or None when this host is not authorized.

        Raises ``insecure_storage`` instead of returning None when a bundle is
        there but stored unsafely, and ``credentials_unreadable`` when it is
        there but cannot be decrypted or parsed: None sends the operator to
        re-login, which fixes neither.
        """
        ...

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        """Store ``bundle``; with ``expected_previous``, only if that is what is stored now.

        A mismatch raises ``CredentialConflict`` and leaves the store untouched.
        """
        ...

    def rebind(
        self, bundle: TokenBundle, *, on_bound: Callable[[TokenBundle], object] | None = None
    ) -> TokenBundle:
        """Store a freshly issued bundle as a new binding, under the refresh lock.

        Mints the ``binding_id`` and drops the cached identity; ``on_bound``
        (say, the registry update) runs with the lock still held. Returns the
        bundle as stored.
        """
        ...

    def clear(self, *, on_cleared: Callable[[], object] | None = None) -> None:
        """Forget the binding (tokens and cached identity), under the refresh lock.

        ``on_cleared`` runs with the lock still held.
        """
        ...

    def refresh_lock(self, timeout: float) -> AbstractAsyncContextManager[None]:
        """Exclusive across processes; not getting it within ``timeout`` is ``lock_timeout``."""
        ...


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
        blob = self._fernet(create=True).encrypt(json.dumps(asdict(bundle)).encode())
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
