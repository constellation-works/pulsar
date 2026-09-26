"""Private file primitives: everything pulsar persists is owner-only.

A token save that dies halfway must never destroy the only refresh token,
so whole-file writes go through a temp file, ``fsync`` and ``rename``. Files
are created 0600 from the first byte rather than chmod-ed afterwards, and
directories 0700, so there is no window in which another local user can
open them.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600


def ensure_private_dir(path: os.PathLike[str] | str) -> Path:
    p = Path(path)
    p.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(p, DIR_MODE)
    return p


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_atomic(path: os.PathLike[str] | str, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically, mode 0600.

    Readers see either the old contents or the new, never a truncated file.
    """
    target = Path(path)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(target.parent)


def append_private(path: os.PathLike[str] | str, text: str) -> None:
    """Append ``text`` to ``path``, creating it 0600 if absent."""
    data = text.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
