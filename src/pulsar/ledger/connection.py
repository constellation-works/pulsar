"""Opening the ledger file: read-write for state changes, read-only for reports.

A read-write open creates the home and the file (0600, before SQLite sees it)
and is what every state change and migration uses. A read-only open creates
nothing, takes no write lock and never migrates, so reports work against a
read-only home.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Callable, Generator
from pathlib import Path

from pulsar.errors import INTERNAL, INVALID_CONFIG, PulsarError
from pulsar.home import Paths, append_private

# A read that raced a checkpoint (see ``read_only``) is retried this often.
READ_ATTEMPTS = 3


def open_writable(paths: Paths, busy_timeout_ms: int) -> sqlite3.Connection:
    """A connection in autocommit mode; callers BEGIN explicitly."""
    path = paths.ledger_db
    paths.ensure()
    # Create the file 0600 ourselves so SQLite never creates it with the
    # process umask; its -wal/-shm files inherit the database's mode.
    append_private(path, "")
    conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        # The submitting row must survive a power cut before the POST goes out.
        conn.execute("PRAGMA synchronous = FULL")
    except BaseException:
        conn.close()
        raise
    return conn


def read_only[T](
    path: Path,
    busy_timeout_ms: int,
    body: Callable[[sqlite3.Connection], T],
    *,
    missing: T,
) -> T:
    """``body`` over a connection that cannot write; ``missing`` if there is no file.

    The file is opened ``mode=ro``. SQLite reads a WAL database through its
    ``-shm`` index, which it creates if absent; in a directory it may not
    write, that fails (``SQLITE_READONLY_DIRECTORY``, or ``SQLITE_CANTOPEN``
    when a ``-wal`` has no ``-shm``). Then:

    - A ``-wal`` file exists: it may hold committed writes the main file does
      not have yet, and only the index can read them. Reading around it
      would silently lose rows, so this refuses with the remedy.
    - No ``-wal``: the last connection checkpointed and removed it, so the
      main file alone holds every committed write, and ``immutable=1`` reads
      it without the index and without locks. That skips locking, so a
      writer that starts meanwhile could checkpoint into pages being read.
      No writer of this user can (it would have to create a ``-wal`` in the
      directory this user cannot write); for anyone else, the file's inode,
      size and mtime are compared before and after, and a changed file is
      read again (up to ``READ_ATTEMPTS`` times) the normal way first.

    In a writable directory SQLite may leave its ``-wal``/``-shm`` pair
    behind after a read-only open (empty; the next writer's close removes
    them). That is SQLite's lock state, not pulsar's: no row, schema or
    journal-mode change is ever written.
    """
    wal = path.with_name(f"{path.name}-wal")
    for _ in range(READ_ATTEMPTS):
        if not path.exists():
            return missing
        try:
            with _connect(path, "mode=ro", busy_timeout_ms) as conn:
                return body(conn)
        except sqlite3.OperationalError as exc:
            if not _directory_not_writable(exc):
                raise
        if wal.exists():
            raise PulsarError(
                INVALID_CONFIG,
                f"cannot read ledger {path}: its write-ahead log {wal} may hold committed "
                f"writes, and SQLite needs to create {path.name}-shm to read them but "
                f"{path.parent} is not writable; make it writable (chmod u+w "
                f"{path.parent}) and retry",
                detail={"path": str(path)},
            )
        before = _fingerprint(path)
        try:
            with _connect(path, "mode=ro&immutable=1", busy_timeout_ms) as conn:
                result = body(conn)
        except sqlite3.DatabaseError as exc:
            if _fingerprint(path) != before:
                continue
            raise PulsarError(
                INVALID_CONFIG,
                f"cannot read ledger {path} from a read-only directory: SQLite "
                f"{exc.sqlite_errorname}",
                detail={"path": str(path)},
            ) from exc
        if _fingerprint(path) == before and not wal.exists():
            return result
    raise PulsarError(
        INTERNAL,
        f"ledger {path} kept changing while it was read from a read-only directory; retry",
        detail={"path": str(path)},
        retryable=True,
    )


@contextlib.contextmanager
def _connect(path: Path, query: str, busy_timeout_ms: int) -> Generator[sqlite3.Connection]:
    uri = f"{path.absolute().as_uri()}?{query}"
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _directory_not_writable(exc: sqlite3.Error) -> bool:
    code: int | None = getattr(exc, "sqlite_errorcode", None)
    return code == sqlite3.SQLITE_READONLY_DIRECTORY or (
        code is not None and code & 0xFF == sqlite3.SQLITE_CANTOPEN
    )


def _fingerprint(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns
