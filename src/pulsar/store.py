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
refresher holds across load → token POST → save.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import shlex
import stat
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken

from .config import Paths
from .errors import API_ERROR, INSECURE_STORAGE, PulsarError
from .fsutil import DIR_MODE, FILE_MODE, write_private_atomic

LOCK_POLL_SECONDS = 0.05


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    scope: str
    client_id: str
    token_type: str = "bearer"

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

    def clear(self) -> None: ...

    def refresh_lock(self, timeout: float) -> AbstractAsyncContextManager[None]:
        """Exclusive across processes; failing to get it within ``timeout`` is ``api_error``."""
        ...


def _insecure(path: Path, problem: str, fix: str) -> PulsarError:
    return PulsarError(
        INSECURE_STORAGE,
        f"{path} {problem}; pulsar will not use X credentials stored there. Fix: {fix}",
        detail={"path": str(path), "fix": fix},
    )


def require_private(path: Path, *, is_dir: bool = False) -> None:
    """Raise ``insecure_storage`` if ``path`` exists and others could read or replace it."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    if st.st_uid != os.geteuid():
        raise _insecure(
            path,
            f"is owned by uid {st.st_uid}, not the current user (uid {os.geteuid()})",
            f"run pulsar as uid {st.st_uid}, or remove {shlex.quote(str(path))} "
            "and re-run `pulsar auth login`",
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        want = DIR_MODE if is_dir else FILE_MODE
        raise _insecure(
            path,
            f"is mode {mode:04o}, open to group/other users",
            f"chmod {want:o} {shlex.quote(str(path))}",
        )


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
        return Fernet(self.paths.key_file.read_bytes().strip())

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
        return TokenBundle(**json.loads(raw))

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        # The compare is atomic with the write only while the caller holds
        # refresh_lock, which every refresher does.
        self._prepare_home()
        if expected_previous is not None and self.load() != expected_previous:
            raise CredentialConflict()
        blob = self._fernet(create=True).encrypt(json.dumps(asdict(bundle)).encode())
        write_private_atomic(self.paths.token_file, blob)

    def clear(self) -> None:
        for p in (self.paths.token_file, self.paths.whoami_cache):
            if p.exists():
                p.unlink()

    @contextlib.asynccontextmanager
    async def refresh_lock(self, timeout: float) -> AsyncIterator[None]:
        self._prepare_home()
        fd = os.open(self.paths.refresh_lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PulsarError(
                            API_ERROR,
                            f"another pulsar process held the token refresh lock for over "
                            f"{timeout:g}s; retry later",
                            detail={"lock": str(self.paths.refresh_lock), "retryable": True},
                        ) from None
                    await asyncio.sleep(LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# The name the rest of the codebase (and callers) have always used.
TokenStore = FernetFileStore
