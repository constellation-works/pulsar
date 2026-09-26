"""``SqliteLedger(read_only=True)``: reports that create, lock and migrate nothing.

Most of these run against a home the test makes read-only (0500), then
compare every file's mode, mtime and digest before and after.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import pulsar.core.ledger.connection as connection_mod
from pulsar.core.errors import INTERNAL, INVALID_CONFIG, PulsarError
from pulsar.core.ledger import SCHEMA_VERSION, SqliteLedger
from pulsar.core.paths import Paths
from pulsar.core.usage import Usage

from .test_ledger import (
    ACCT,
    DAY,
    ME,
    MONTH,
    build_v1_ledger,
    claim_plan,
    intents,
    send_all,
    sql,
)

pytestmark = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)

Snapshot = dict[str, tuple[int, int, str]]


def snapshot(home: Path) -> Snapshot:
    """Every path under ``home`` (and ``home`` itself): mode, mtime, content digest."""
    if not home.exists():
        return {}
    out: Snapshot = {}
    for p in sorted([home, *home.rglob("*")]):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
        out[str(p.relative_to(home.parent))] = (stat.S_IMODE(st.st_mode), st.st_mtime_ns, digest)
    return out


@contextlib.contextmanager
def read_only_home(paths: Paths) -> Iterator[None]:
    os.chmod(paths.home, 0o500)
    try:
        yield
    finally:
        os.chmod(paths.home, 0o700)


def every_read(ledger: SqliteLedger) -> dict[str, Any]:
    return {
        "get": ledger.get("delete:9"),
        "get_plan": ledger.get_plan("thread"),
        "known_post_ids": ledger.known_post_ids(["500", "9", "nope"]),
        "history": ledger.history(limit=50),
        "count": ledger.count(),
        "count_alias": ledger.count(account_alias=ACCT.alias),
        "last_published": ledger.last_published(ACCT.alias),
        "usage": ledger.usage(ACCT.alias, day_start=DAY, month_start=MONTH),
        "unresolved": ledger.unresolved(stale_after=timedelta(0), now=DAY + timedelta(days=1)),
    }


EMPTY = {
    "get": None,
    "get_plan": None,
    "known_post_ids": set[str](),
    "history": [],
    "count": 0,
    "count_alias": 0,
    "last_published": None,
    "usage": Usage(0.0, 0.0, 0),
    "unresolved": [],
}


def populate(paths: Paths) -> None:
    ledger = SqliteLedger(paths)
    claim_plan(ledger, "thread", n=2)
    send_all(ledger, "thread", 2)
    claim_plan(ledger, "pending")
    ledger.claim(key="delete:9", tool="delete_post", digest="d", account=ME, caller="t")
    ledger.publish("delete:9", post_id="9", meta={"deleted": True})


def spy_on_opens(monkeypatch) -> list[str]:
    """The URI query of every read-only open, in order."""
    opened: list[str] = []
    real = connection_mod._connect

    def spy(path, query, busy_timeout_ms):
        opened.append(query)
        return real(path, query, busy_timeout_ms)

    monkeypatch.setattr(connection_mod, "_connect", spy)
    return opened


def test_every_read_in_a_read_only_home_changes_nothing(paths, monkeypatch):
    populate(paths)
    expected = every_read(SqliteLedger(paths))
    assert not paths.ledger_db.with_name("ledger.sqlite3-wal").exists(), "fully checkpointed"
    opened = spy_on_opens(monkeypatch)
    with read_only_home(paths):
        before = snapshot(paths.home)
        got = every_read(SqliteLedger(paths, read_only=True))
        after = snapshot(paths.home)
    assert after == before
    assert got == expected
    assert got["count"] == 3 and got["last_published"].key == "delete:9"
    # SQLite cannot create its index here, so each read fell back to the immutable open.
    assert opened == ["mode=ro", "mode=ro&immutable=1"] * len(expected)


def test_a_file_that_changes_during_an_immutable_read_is_read_again(paths, monkeypatch):
    populate(paths)
    stamps = iter(range(100))
    monkeypatch.setattr(connection_mod, "_fingerprint", lambda _path: next(stamps))
    opened = spy_on_opens(monkeypatch)
    with read_only_home(paths), pytest.raises(PulsarError) as exc:
        SqliteLedger(paths, read_only=True).count()
    assert exc.value.code == INTERNAL and exc.value.retryable is True
    assert opened == ["mode=ro", "mode=ro&immutable=1"] * connection_mod.READ_ATTEMPTS

    seen = iter([1, 2, 3, 3, 3])  # changed during the first read only
    monkeypatch.setattr(connection_mod, "_fingerprint", lambda _path: next(seen))
    opened.clear()
    with read_only_home(paths):
        assert SqliteLedger(paths, read_only=True).count() == 3
    assert opened == ["mode=ro", "mode=ro&immutable=1"] * 2


def test_reads_see_the_write_ahead_log_while_a_writer_holds_it(paths):
    SqliteLedger(paths).migrate()
    holder = sqlite3.connect(paths.ledger_db)  # keeps the log from being checkpointed away
    try:
        holder.execute("SELECT count(*) FROM writes").fetchall()
        populate(paths)
        wal = paths.ledger_db.with_name("ledger.sqlite3-wal")
        assert wal.stat().st_size > 0, "the rows are in the log, not the main file"
        expected = every_read(SqliteLedger(paths))
        with read_only_home(paths):
            names = sorted(p.name for p in paths.home.iterdir())
            db, log = paths.ledger_db.read_bytes(), wal.read_bytes()
            got = every_read(SqliteLedger(paths, read_only=True))
            assert sorted(p.name for p in paths.home.iterdir()) == names
            assert (paths.ledger_db.read_bytes(), wal.read_bytes()) == (db, log)
        assert got == expected
    finally:
        holder.close()


def test_a_log_without_its_index_in_a_read_only_home_is_refused_with_the_remedy(paths, tmp_path):
    SqliteLedger(paths).migrate()
    holder = sqlite3.connect(paths.ledger_db)
    try:
        holder.execute("SELECT count(*) FROM writes").fetchall()
        populate(paths)
        copy = Paths(tmp_path / "copied-home")
        copy.home.mkdir(mode=0o700)
        for name in ("ledger.sqlite3", "ledger.sqlite3-wal"):  # no -shm
            shutil.copy(paths.home / name, copy.home / name)
    finally:
        holder.close()
    with read_only_home(copy):
        before = snapshot(copy.home)
        with pytest.raises(PulsarError) as exc:
            SqliteLedger(copy, read_only=True).history()
        assert snapshot(copy.home) == before
    assert exc.value.code == INVALID_CONFIG
    assert str(copy.ledger_db) in exc.value.message and "chmod u+w" in exc.value.message


@pytest.mark.parametrize("home_exists", [True, False], ids=["empty-home", "no-home"])
def test_a_missing_ledger_reads_empty_and_creates_nothing(paths, home_exists):
    if home_exists:
        paths.home.mkdir(mode=0o700)
        with read_only_home(paths):
            before = snapshot(paths.home)
            got = every_read(SqliteLedger(paths, read_only=True))
            assert snapshot(paths.home) == before
    else:
        got = every_read(SqliteLedger(paths, read_only=True))
        assert not paths.home.exists()
    assert got == EMPTY


def test_a_file_no_schema_was_applied_to_reads_empty(paths):
    paths.home.mkdir(mode=0o700)
    paths.ledger_db.write_bytes(b"")
    with read_only_home(paths):
        assert every_read(SqliteLedger(paths, read_only=True)) == EMPTY


def test_an_older_schema_is_refused_with_the_migrate_remedy_and_left_alone(paths):
    build_v1_ledger(paths)
    before = snapshot(paths.home)  # writable home: nothing may change here either
    with pytest.raises(PulsarError) as exc:
        SqliteLedger(paths, read_only=True).history()
    assert snapshot(paths.home) == before
    assert exc.value.code == INVALID_CONFIG
    assert str(paths.ledger_db) in exc.value.message and "pulsar migrate" in exc.value.message
    assert exc.value.detail == {
        "path": str(paths.ledger_db),
        "schema_version": 1,
        "supported": SCHEMA_VERSION,
    }
    assert sql(paths, "PRAGMA user_version") == [(1,)]
    assert sql(paths, "PRAGMA journal_mode") == [("delete",)], "never switched to WAL"


def test_a_newer_schema_is_refused(paths):
    SqliteLedger(paths).migrate()
    conn = sqlite3.connect(paths.ledger_db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with read_only_home(paths), pytest.raises(PulsarError) as exc:
        SqliteLedger(paths, read_only=True).get("k")
    assert exc.value.code == INVALID_CONFIG and f"v{SCHEMA_VERSION + 1}" in exc.value.message


def _mutations(ledger: SqliteLedger) -> list[Callable[[], object]]:
    err = PulsarError("api_error", "down")
    return [
        ledger.migrate,
        lambda: ledger.claim(key="k", tool="delete_post", digest="d", account=ME, caller="t"),
        lambda: ledger.publish("k", post_id="1"),
        lambda: ledger.fail("k", err),
        lambda: ledger.claim_plan(
            key="p",
            tool="publish",
            digest="d",
            provider="x",
            account=ACCT,
            caller="t",
            items=intents(1),
            admit=None,
            day_start=DAY,
            month_start=MONTH,
        ),  # fmt: skip
        lambda: ledger.begin_item("p", 0),
        lambda: ledger.item_sending("p", 0, "stamp"),
        lambda: ledger.item_published("p", 0, post_id="1", url="u"),
        lambda: ledger.item_failed("p", 0, err),
        lambda: ledger.item_unknown("p", 0, err),
        lambda: ledger.finish("p"),
        lambda: ledger.settle("p", seen={}, verdicts={}),
        lambda: ledger.skip(key="s", provider="x", account=ACCT, caller="t", note=None),
        lambda: ledger.record_import(
            key="i",
            tool="import:posted.jsonl",
            digest="d",
            provider="x",
            account=ACCT,
            caller="t",
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
            post_id="1",
            url="u",
            text_sha256=None,
            note=None,
            meta={},
        ),  # fmt: skip
    ]


def test_every_state_change_on_a_read_only_ledger_raises(paths):
    ledger = SqliteLedger(paths, read_only=True)
    for mutate in _mutations(ledger):
        with pytest.raises(PulsarError) as exc:
            mutate()
        assert exc.value.code == INTERNAL and "read-only" in exc.value.message
    assert not paths.home.exists()
