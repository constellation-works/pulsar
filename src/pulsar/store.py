"""Where the X token bundle lives, and what that storage does and does not protect.

``CredentialStore`` is the interface the rest of pulsar codes against.
``TokenStore`` (``FernetFileStore``) is today's implementation: a Fernet
ciphertext (``tokens.enc``) beside a host-local key file (``key``), both 0600
in a 0700 pulsar home. Orbit's host-held secrets (ORB-13009) are meant to drop
in behind the same protocol, which is why ``save`` already takes an
``expected_previous`` bundle for compare-and-swap rotation.

What the file store protects against: the bundle showing up in plaintext in
a backup, a ``cat`` or ``grep`` over the home directory, a stray ``git add``,
or being read by *another* local user. Loading refuses a home directory
wider than 0700, or a key or bundle wider than 0600 or owned by someone else
(``insecure_storage``), rather than quietly using a credential others could
have copied.

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
a logout.

Each login mints a ``binding_id`` that refreshes carry forward. The cached
identity (``whoami.json``) records the binding it describes and is ignored
once that no longer matches the stored bundle, so a lookup that raced a
re-login cannot keep naming the old account.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .config import Paths
from .errors import API_ERROR, INSECURE_STORAGE, AuthExpired, PulsarError
from .fsutil import FILE_MODE, require_private, write_private_atomic

LOCK_POLL_SECONDS = 0.05
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
        cls, data: dict, *, client_id: str, now: float | None = None
    ) -> TokenBundle:
        now = time.time() if now is None else now
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=now + float(data.get("expires_in", 7200)),
            scope=data.get("scope", ""),
            client_id=client_id,
            token_type=data.get("token_type", "bearer"),
        )


def cached_identity(paths: Paths, bundle: TokenBundle) -> dict[str, str] | None:
    """The cached ``{user_id, username}`` if it describes ``bundle``'s binding, else None."""
    try:
        cached = json.loads(paths.whoami_cache.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if not isinstance(cached, dict) or not {"user_id", "username"} <= cached.keys():
        return None
    if cached.get("binding_id") != bundle.binding_id:
        return None
    return {"user_id": str(cached["user_id"]), "username": str(cached["username"])}


def save_identity(paths: Paths, binding_id: str | None, me: dict[str, str]) -> None:
    """Cache ``me`` as the identity of ``binding_id`` (the bundle it was looked up with)."""
    record = {**me, "binding_id": binding_id}
    write_private_atomic(paths.whoami_cache, (json.dumps(record) + "\n").encode())


class CredentialConflict(PulsarError):
    """A compare-and-swap save found a different bundle than the caller expected."""

    def __init__(self) -> None:
        super().__init__(
            API_ERROR, "the stored X credential changed during refresh; retry the call"
        )


class CredentialStore(Protocol):
    """What pulsar needs from wherever the token bundle is kept."""

    def exists(self) -> bool: ...

    def load(self) -> TokenBundle | None:
        """The stored bundle, or None when this host is not authorized.

        Raises ``insecure_storage`` instead of returning None when a bundle is
        there but stored unsafely: None sends the operator to re-login, which
        does not fix a permissions problem.
        """
        ...

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        """Store ``bundle``; with ``expected_previous``, only if that is what is stored now.

        A mismatch raises ``CredentialConflict`` and leaves the store untouched.
        """
        ...

    def rebind(self, bundle: TokenBundle) -> TokenBundle:
        """Store a freshly issued bundle as a new binding, under the refresh lock.

        Mints the ``binding_id`` and drops the cached identity. Returns the
        bundle as stored.
        """
        ...

    def clear(self) -> None:
        """Forget the binding (tokens and cached identity), under the refresh lock."""
        ...

    def refresh_lock(self, timeout: float) -> AbstractAsyncContextManager[None]:
        """Exclusive across processes; failing to get it within ``timeout`` is ``api_error``."""
        ...


class FernetFileStore:
    """``CredentialStore`` on local files: Fernet ciphertext beside its key."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    # -- permissions --------------------------------------------------------

    def _check(self) -> None:
        require_private(self.paths.home, is_dir=True)
        require_private(self.paths.key_file)
        require_private(self.paths.token_file)

    def _prepare_home(self) -> None:
        """Create the home 0700 if missing; refuse (never silently fix) an unsafe one."""
        if self.paths.home.exists():
            self._check()
        else:
            self.paths.ensure()

    # -- key ----------------------------------------------------------------

    def _read_key(self) -> Fernet:
        try:
            return Fernet(self.paths.key_file.read_bytes().strip())
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

        The key is written complete to a private temp file and published with
        ``link()``, which like ``O_CREAT|O_EXCL`` fails if the name exists, but
        never exposes a half-written key. The loser reads the winner's key, so
        two processes can never encrypt under different keys.
        """
        key_file = self.paths.key_file
        fd, tmp = tempfile.mkstemp(dir=key_file.parent, prefix=".key.", suffix=".tmp")
        try:
            os.fchmod(fd, FILE_MODE)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
                fh.flush()
                os.fsync(fh.fileno())
            with contextlib.suppress(FileExistsError):
                os.link(tmp, key_file)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        require_private(key_file)
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
        return self.paths.token_file.exists() and self.paths.key_file.exists()

    def load(self) -> TokenBundle | None:
        if not self.paths.home.exists():
            return None
        self._check()
        if not self.exists():
            return None
        try:
            raw = self._fernet(create=False).decrypt(self.paths.token_file.read_bytes())
        except (InvalidToken, FileNotFoundError):
            return None
        try:
            return TokenBundle(**json.loads(raw))
        except (ValueError, TypeError) as exc:
            raise AuthExpired(
                f"the stored X token bundle is unreadable ({exc.__class__.__name__}); "
                "a human must re-run `pulsar auth login`"
            ) from exc

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        # The compare is atomic with the write only while the caller holds
        # refresh_lock, which every refresher does.
        self._prepare_home()
        if expected_previous is not None and self.load() != expected_previous:
            raise CredentialConflict()
        blob = self._fernet(create=True).encrypt(json.dumps(asdict(bundle)).encode())
        write_private_atomic(self.paths.token_file, blob)

    def rebind(self, bundle: TokenBundle) -> TokenBundle:
        bundle.binding_id = uuid.uuid4().hex
        with self.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS):
            self.save(bundle)
            self._drop_identity()
        return bundle

    def clear(self) -> None:
        if not self.paths.home.exists():
            return
        with self.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS):
            if self.paths.token_file.exists():
                self.paths.token_file.unlink()
            self._drop_identity()

    def _drop_identity(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.paths.whoami_cache.unlink()

    # -- refresh lock -------------------------------------------------------

    def _lock_fd(self) -> int:
        self._prepare_home()
        return os.open(self.paths.refresh_lock, os.O_RDWR | os.O_CREAT, FILE_MODE)

    def _try_lock(self, fd: int, deadline: float, timeout: float) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise PulsarError(
                    API_ERROR,
                    f"another pulsar process held the token refresh lock for over "
                    f"{timeout:g}s; retry later",
                    detail={"lock": str(self.paths.refresh_lock), "retryable": True},
                ) from None
            return False

    @contextlib.asynccontextmanager
    async def refresh_lock(self, timeout: float) -> AsyncIterator[None]:
        fd = self._lock_fd()
        try:
            deadline = time.monotonic() + timeout
            while not self._try_lock(fd, deadline, timeout):
                await asyncio.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def refresh_lock_sync(self, timeout: float) -> Iterator[None]:
        """``refresh_lock`` for the synchronous CLI paths (login, logout)."""
        fd = self._lock_fd()
        try:
            deadline = time.monotonic() + timeout
            while not self._try_lock(fd, deadline, timeout):
                time.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# The name the rest of the codebase (and callers) have always used.
TokenStore = FernetFileStore
