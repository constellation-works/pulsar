"""The ledger file's schema: migrations, version skew, and lock classification."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from pulsar.core.errors import INVALID_CONFIG, OUTCOME_UNKNOWN, OutcomeUnknown, PulsarError
from pulsar.core.ledger import FAILED, PENDING, PUBLISHED, SCHEMA_VERSION, UNKNOWN, SqliteLedger
from pulsar.core.ledger.schema import is_busy
from pulsar.core.paths import Paths

from .test_ledger import DAY, ME, T0, build_v1_ledger, claim_plan, sha, sql


def schema_of(paths) -> list[tuple[str, str, str, str | None]]:
    return sql(paths, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")


def test_v1_database_migrates_in_place(paths):
    build_v1_ledger(paths)
    ledger = SqliteLedger(paths)
    before = ["k-pub", "k-unk", "k-fail", "delete:9", "upload:u1"]
    assert all(ledger.get(key) is not None for key in before)
    assert ledger.count() == len(before)

    assert sql(paths, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert SCHEMA_VERSION == 2
    names = {r[1] for r in schema_of(paths)}
    assert {"writes_state", "writes_post_id", "writes_account", "writes_alias"} <= names
    assert {"items", "items_state", "items_submitted"} <= names
    assert "writes_v2" not in names

    # v1 readers see the same rows.
    pub = ledger.get("k-pub")
    assert pub is not None
    assert pub.state == PUBLISHED and pub.post_id == "101" and pub.created_at == T0
    upload, failed = ledger.get("upload:u1"), ledger.get("k-fail")
    assert upload is not None and upload.meta == {"bytes": 3, "mime": "image/png"}
    assert failed is not None and failed.attempts == 2

    # Every create_post row gained the one item it describes; other tools none.
    plan = ledger.get_plan("k-pub")
    assert plan.provider == "x" and plan.account_alias == "x:constworks"
    assert plan.digest is None and plan.request_digest == "d-pub"
    (item,) = plan.items
    assert (item.idx, item.state, item.post_id, item.url) == (
        0, PUBLISHED, "101", "https://x.com/constworks/status/101"
    )  # fmt: skip
    assert item.text_sha256 == sha("hello") and item.est_cost_usd == 0
    assert item.submitted_at == T0
    unk = ledger.get_plan("k-unk").items[0]
    assert unk.state == UNKNOWN and unk.error_code == OUTCOME_UNKNOWN and unk.retryable is False
    assert ledger.get_plan("k-fail").items[0].state == FAILED
    assert ledger.get_plan("delete:9").items == ()
    assert ledger.get_plan("upload:u1").items == ()

    # The widened CHECK accepts the new states, and v1 behaviour is intact.
    assert claim_plan(ledger, "plan-new").state == PENDING
    replay = ledger.claim(key="k-pub", tool="create_post", digest="d-pub", account=ME, caller="b")
    assert replay.state == PUBLISHED and replay.post_id == "101"
    with pytest.raises(OutcomeUnknown):
        ledger.claim(key="k-unk", tool="create_post", digest="d-unk", account=ME, caller="b")
    # The unknown v1 row is reconcile's to settle.
    assert [r.key for r in ledger.unresolved(stale_after=timedelta(0), now=DAY)] == ["k-unk"]


def test_v1_database_new_ids_do_not_reuse_old_ones(paths):
    build_v1_ledger(paths)
    ledger = SqliteLedger(paths)
    claim_plan(ledger, "after-migration")
    ids = [r[0] for r in sql(paths, "SELECT id FROM writes ORDER BY id")]
    orphans = sql(paths, "SELECT count(*) FROM items WHERE write_id NOT IN (SELECT id FROM writes)")
    assert ids == [1, 2, 3, 4, 5, 6] and orphans == [(0,)]


def test_fresh_and_upgraded_ledgers_have_identical_schemas(paths, tmp_path):
    """A new file and one migrated from v1 end the same."""
    build_v1_ledger(paths)
    SqliteLedger(paths).migrate()
    fresh = Paths(tmp_path / "fresh-home")
    SqliteLedger(fresh).migrate()
    assert schema_of(fresh) == schema_of(paths)
    assert sql(fresh, "PRAGMA user_version") == sql(paths, "PRAGMA user_version")


def test_migrate_reports_the_versions_it_moved_between(paths, tmp_path):
    build_v1_ledger(paths)
    ledger = SqliteLedger(paths)
    assert ledger.migrate() == (1, SCHEMA_VERSION)
    assert ledger.migrate() == (SCHEMA_VERSION, SCHEMA_VERSION)
    new = Paths(tmp_path / "new-home")
    assert SqliteLedger(new).migrate() == (0, SCHEMA_VERSION)
    assert new.ledger_db.exists()


def test_newer_schema_is_refused(paths):
    SqliteLedger(paths).get("x")
    bump(paths, SCHEMA_VERSION + 1)
    with pytest.raises(PulsarError) as exc:
        SqliteLedger(paths).get("x")
    assert exc.value.code == INVALID_CONFIG
    with pytest.raises(PulsarError) as exc:
        SqliteLedger(paths).migrate()
    assert exc.value.code == INVALID_CONFIG
    assert sql(paths, "PRAGMA user_version") == [(SCHEMA_VERSION + 1,)], "never migrated down"


def test_a_running_ledger_stops_once_a_newer_pulsar_migrates_the_file(paths):
    """The check is per connection, not once per process."""
    ledger = SqliteLedger(paths)
    ledger.claim(key="k", tool="delete_post", digest="d", account=ME, caller="t")
    bump(paths, SCHEMA_VERSION + 1)
    with pytest.raises(PulsarError) as write:
        ledger.publish("k", post_id="1")
    with pytest.raises(PulsarError) as read:
        ledger.get("k")
    for exc in (write.value, read.value):
        assert exc.code == INVALID_CONFIG
        assert str(paths.ledger_db) in exc.message
        assert f"v{SCHEMA_VERSION + 1}" in exc.message and f"v{SCHEMA_VERSION}" in exc.message
    bump(paths, SCHEMA_VERSION)
    record = ledger.get("k")
    assert record is not None and record.state == "submitting", "the refused write wrote nothing"


def bump(paths, version: int) -> None:
    conn = sqlite3.connect(paths.ledger_db)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
    finally:
        conn.close()


def test_busy_is_classified_from_the_result_code(paths):
    """Not from the message text."""
    SqliteLedger(paths).migrate()
    holder = sqlite3.connect(paths.ledger_db, isolation_level=None)
    other = sqlite3.connect(paths.ledger_db, timeout=0, isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as locked:
            other.execute("BEGIN EXCLUSIVE")
        assert is_busy(locked.value)
        holder.execute("ROLLBACK")
        # A message that mentions "locked" but is not a lock error.
        with pytest.raises(sqlite3.OperationalError) as missing:
            other.execute("SELECT * FROM locked")
        assert "locked" in str(missing.value) and not is_busy(missing.value)
    finally:
        holder.close()
        other.close()
