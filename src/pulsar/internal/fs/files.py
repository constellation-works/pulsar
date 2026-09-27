"""Private file primitives: everything pulsar persists is owner-only.

A token save that dies halfway must never destroy the only refresh token,
so whole-file writes go through a temp file, ``fsync``, ``rename`` and an
``fsync`` of the directory (``write_private_atomic``, or
``publish_new_private`` for a file that must be created exactly once). Files
are created 0600 from the first byte rather than chmod-ed afterwards, and
directories 0700, so there is no window in which another local user can
open them.

State is validated where it is loaded: ``require_private``
refuses a path that is a symlink, owned by another user, or open to group
and other users, and never follows a symlink to decide (``lstat``). Writes
never follow a symlink in the final component either.

Cross-process locks (``hold_lock``, ``hold_lock_async``) are ``flock``s taken
against a deadline. The holder writes ``{pid, label, acquired_at}`` into the
lock file right after acquiring, so a waiter that times out can say who holds
it. That record is diagnostic only: ownership is the ``flock``.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shlex
import stat
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulsar.internal.errors import INSECURE_STORAGE, LOCK_TIMEOUT, PulsarError

from .jsonx import as_object

DIR_MODE = 0o700
FILE_MODE = 0o600
LOCK_POLL_SECONDS = 0.05
# The holder record is a few dozen bytes; anything longer is not one.
_HOLDER_MAX_BYTES = 4096

CREDENTIALS_AT_RISK = "pulsar will not use X credentials stored there"


def _insecure(
    path: os.PathLike[str] | str, problem: str, fix: str, consequence: str = CREDENTIALS_AT_RISK
) -> PulsarError:
    return PulsarError(
        INSECURE_STORAGE,
        f"{path} {problem}; {consequence}. Fix: {fix}",
        detail={"path": str(path), "fix": fix},
    )


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_private(
    path: os.PathLike[str] | str,
    *,
    is_dir: bool = False,
    readable: bool = False,
    symlink_fix: str | None = None,
    consequence: str = CREDENTIALS_AT_RISK,
) -> None:
    """Raise ``insecure_storage`` if ``path`` exists and others could read or replace it.

    Refused: a symlink (never followed, whatever it points at), an owner
    other than the current user, and any group/other permission bit. With
    ``readable`` (state that is not secret but steers pulsar, such as
    ``config.toml``) group/other may read, but not write. ``symlink_fix``
    replaces the default remedy for a symlink.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    quoted = shlex.quote(str(path))
    if stat.S_ISLNK(st.st_mode):
        target = os.path.realpath(path)
        kind = "directory" if is_dir else "file"
        raise _insecure(
            path,
            f"is a symlink (to {target}); pulsar does not follow symlinks to its state",
            symlink_fix
            or f"replace the symlink with the real {kind}: mv {shlex.quote(target)} {quoted}",
            consequence,
        )
    if st.st_uid != os.geteuid():
        raise _insecure(
            path,
            f"is owned by uid {st.st_uid}, not the current user (uid {os.geteuid()})",
            f"run pulsar as uid {st.st_uid}, or remove {quoted} and re-run `pulsar auth login`",
            consequence,
        )
    mode = stat.S_IMODE(st.st_mode)
    if readable and mode & 0o022:
        raise _insecure(
            path,
            f"is mode {mode:04o}, writable by group/other users",
            f"chmod go-w {quoted}",
            consequence,
        )
    if not readable and mode & 0o077:
        want = DIR_MODE if is_dir else FILE_MODE
        raise _insecure(
            path,
            f"is mode {mode:04o}, open to group/other users",
            f"chmod {want:o} {quoted}",
            consequence,
        )


def fsync_dir(path: os.PathLike[str] | str) -> None:
    """Make a rename, link, unlink or mkdir inside ``path`` durable."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def ensure_private_dir(path: os.PathLike[str] | str, *, symlink_fix: str | None = None) -> Path:
    """Create ``path`` 0700 if missing; refuse (never silently fix) an existing unsafe one.

    Quietly chmod-ing a wide directory would hide the fact that its contents
    were exposed for however long it was wide; the operator should know.
    Missing parents are created 0700 too, and each new directory's parent is
    fsynced so the directory survives a crash.
    """
    p = Path(path)
    require_private(p, is_dir=True, symlink_fix=symlink_fix)
    if p.is_dir():
        return p
    if p.parent != p and not p.parent.exists():
        ensure_private_dir(p.parent)
    try:
        os.mkdir(p, DIR_MODE)
    except FileExistsError:
        # Created by a racing process between the check and the mkdir.
        require_private(p, is_dir=True, symlink_fix=symlink_fix)
        return p
    os.chmod(p, DIR_MODE)  # mkdir's mode is masked by the umask
    fsync_dir(p.parent)
    return p


def _refuse_symlink(path: Path) -> None:
    """``insecure_storage`` if ``path`` is a symlink: a write must never follow one."""
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISLNK(os.lstat(path).st_mode):
            require_private(path)  # raises, naming the target and the fix


def _write_temp(target: Path, data: bytes) -> str:
    """A complete, fsynced 0600 temp file beside ``target``; its path."""
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        try:
            os.fchmod(fd, FILE_MODE)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return tmp


def write_private_atomic(path: os.PathLike[str] | str, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, mode 0600.

    Readers see either the old contents or the new, never a truncated file.
    A symlink at ``path`` is refused, not replaced or written through.
    """
    target = Path(path)
    _refuse_symlink(target)
    tmp = _write_temp(target, data)
    try:
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    fsync_dir(target.parent)


def publish_new_private(path: os.PathLike[str] | str, data: bytes) -> None:
    """Create ``path`` holding ``data``, mode 0600, only if it does not exist yet.

    The data is written complete to a temp file and published with
    ``link()``, which like ``O_CREAT|O_EXCL`` fails if the name exists
    (``FileExistsError``), but never exposes a half-written file. File and
    directory are fsynced, so a published file survives a crash.
    """
    target = Path(path)
    tmp = _write_temp(target, data)
    try:
        os.link(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        fsync_dir(target.parent)


def _open_nofollow(path: os.PathLike[str] | str, flags: int) -> int:
    try:
        return os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            require_private(path)  # a symlink: raises insecure_storage naming it
        raise


def append_private(path: os.PathLike[str] | str, text: str) -> None:
    """Append ``text`` to ``path``, creating it 0600 if absent; never through a symlink."""
    data = text.encode("utf-8")
    fd = _open_nofollow(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)


# -- cross-process locks ------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_holder(fd: int, label: str) -> None:
    """Record who holds the lock. Diagnostic only; a failure here never fails the caller."""
    record = json.dumps({"pid": os.getpid(), "label": label, "acquired_at": _now()})
    with contextlib.suppress(OSError):
        os.ftruncate(fd, 0)
        os.pwrite(fd, record.encode() + b"\n", 0)


def read_holder(fd: int) -> dict[str, Any] | None:
    """The last holder record in the lock file, or None if there is none (or it is garbage).

    It may be stale: the holder writes it just after acquiring, so a waiter
    can briefly read the previous holder's record.
    """
    try:
        raw = os.pread(fd, _HOLDER_MAX_BYTES, 0)
        record = as_object(json.loads(raw))
    except (OSError, ValueError):
        return None
    if record is None:
        return None
    return {k: record[k] for k in ("pid", "label", "acquired_at") if k in record}


def _lock_timeout(path: Path, fd: int, what: str, timeout: float) -> PulsarError:
    holder = read_holder(fd)
    if holder:
        who = (
            f"pid {holder.get('pid', '?')} ({holder.get('label', 'no label')}, "
            f"since {holder.get('acquired_at', '?')})"
        )
    else:
        who = "an unknown holder"
    return PulsarError(
        LOCK_TIMEOUT,
        f"{what} ({path}) was held by {who} for over {timeout:g}s; retry later, and if it "
        "persists check whether that process is stuck",
        detail={"lock": str(path), "holder": holder, "waited_seconds": timeout},
    )


def _try_lock(fd: int, path: Path, what: str, deadline: float, timeout: float) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if time.monotonic() >= deadline:
            raise _lock_timeout(path, fd, what, timeout) from None
        return False
    return True


def open_lock_file(path: os.PathLike[str] | str) -> int:
    """Open (creating 0600) a lock file: close-on-exec, never through a symlink."""
    return _open_nofollow(path, os.O_RDWR | os.O_CREAT)


@contextlib.contextmanager
def hold_lock(path: Path, *, label: str, what: str, timeout: float) -> Generator[None]:
    """Hold an exclusive ``flock`` on ``path``; ``lock_timeout`` after ``timeout`` seconds.

    ``what`` names the lock in the timeout message; ``label`` goes into the
    holder record so a waiter can tell what holds it.
    """
    fd = open_lock_file(path)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd, path, what, deadline, timeout):
            time.sleep(LOCK_POLL_SECONDS)
        _write_holder(fd, label)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.asynccontextmanager
async def hold_lock_async(
    path: Path, *, label: str, what: str, timeout: float
) -> AsyncGenerator[None]:
    """``hold_lock`` for the event loop: waits with ``asyncio.sleep`` between polls."""
    fd = open_lock_file(path)
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(fd, path, what, deadline, timeout):
            await asyncio.sleep(LOCK_POLL_SECONDS)
        _write_holder(fd, label)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
