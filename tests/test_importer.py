"""Importing the retired x-updates routine's posted.jsonl (synthetic fixture only)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pulsar.app.core.ledger import (
    IMPORT_TOOL,
    PENDING,
    PUBLISHED,
    SKIPPED,
    AccountRef,
    ItemIntent,
    SqliteLedger,
    request_digest,
)
from pulsar.app.importer import IMPORT_CALLER, import_posted
from pulsar.app.writelog import WriteLog
from pulsar.internal.errors import PulsarError

FIXTURE = Path(__file__).parent / "fixtures" / "posted.jsonl"
ACCT = AccountRef(alias="x:constworks", provider="x", user_id="1234567890", handle="constworks")
OTHER = AccountRef(alias="x:someone", provider="x", user_id="999", handle="someone")
DAY = datetime(2026, 9, 26, tzinfo=UTC)
MONTH = datetime(2026, 9, 1, tzinfo=UTC)

PUBLISHED_KEYS = {
    "repo:example": "1000000000000000001",
    "pr:example:5": "1000000000000000002",
    "release:orbit:v0.22.1": "1000000000000000004",
    "pr:example:10": "1000000000000000005",
}
SKIPPED_KEYS = {"release:orbit:v0.20.0", "release:orbit:v0.21.0", "pr:example:4"}


def url_for(post_id: str) -> str:
    return f"https://x.com/constworks/status/{post_id}"


def lines() -> list[dict]:
    return [json.loads(raw) for raw in FIXTURE.read_text().splitlines() if raw.strip()]


def run(ledger, path=FIXTURE, account=ACCT):
    return import_posted(ledger, path, account=account, url_for=url_for)


def claim(ledger, key, *, account=ACCT, admit=None):
    return ledger.claim_plan(
        key=key,
        tool="publish",
        digest="sha256:" + "0" * 64,
        provider=account.provider,
        account=account,
        caller="routine",
        items=[ItemIntent("f" * 64, "fp", 0.2)],
        admit=admit,
        day_start=DAY,
        month_start=MONTH,
    )


def test_fixture_imports_every_row_shape(paths):
    ledger = SqliteLedger(paths)
    report = run(ledger)
    assert (report.imported_published, report.imported_skipped) == (4, 3)
    assert report.already_present == 0 and report.conflicts == [] and report.errors == []

    by_key = {line["key"]: line for line in lines()}
    for key, post_id in PUBLISHED_KEYS.items():
        row = ledger.get_plan(key)
        assert row.state == PUBLISHED and row.tool == IMPORT_TOOL and row.caller == IMPORT_CALLER
        assert row.provider == "x" and row.account_alias == "x:constworks"
        assert row.account_user_id == "1234567890" and row.attempts == 1
        assert row.request_digest == request_digest(IMPORT_TOOL, key=key, post_id=post_id)
        assert row.digest is None
        assert (row.post_id, row.url) == (post_id, url_for(post_id))
        (item,) = row.items
        assert (item.state, item.post_id, item.url) == (PUBLISHED, post_id, url_for(post_id))
        assert item.text_sha256 == hashlib.sha256(by_key[key]["text"].encode()).hexdigest()
        assert item.est_cost_usd == 0
        ts = datetime.fromisoformat(by_key[key]["ts"]).astimezone(UTC)
        expected = ts.isoformat(timespec="milliseconds")
        assert row.created_at == item.submitted_at == expected
    for key in SKIPPED_KEYS:
        row = ledger.get_plan(key)
        assert row.state == SKIPPED and row.items == () and row.post_id is None
        assert row.note == by_key[key]["note"]


def test_a_dry_run_reports_the_same_counts_and_writes_nothing(paths):
    ledger = SqliteLedger(paths)
    dry = import_posted(ledger, FIXTURE, account=ACCT, url_for=url_for, apply=False)
    assert dry.applied is False and dry.to_dict()["applied"] is False
    assert ledger.history() == []
    real = run(ledger)
    assert real.applied is True
    assert (dry.imported_published, dry.imported_skipped) == (
        real.imported_published,
        real.imported_skipped,
    )
    again = import_posted(ledger, FIXTURE, account=ACCT, url_for=url_for, apply=False)
    assert again.already_present == real.imported_published + real.imported_skipped


def test_a_dry_run_classifies_a_repeated_key_as_the_real_run_does(paths, tmp_path):
    rows = lines()
    same = rows[0]
    changed = {**rows[1], "post_id": "1000000000000000777"}
    path = tmp_path / "posted.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in [*rows, same, changed]) + "\n")

    def counts(report):
        keys = [(n, k) for n, k, _ in report.conflicts]
        return (report.imported_published, report.imported_skipped, report.already_present, keys)

    dry = import_posted(SqliteLedger(paths), path, account=ACCT, url_for=url_for, apply=False)
    real = run(SqliteLedger(paths), path)
    assert counts(dry) == counts(real)
    assert real.already_present == 1 and len(real.conflicts) == 1


def test_timestamps_are_normalised_to_utc(paths):
    ledger = SqliteLedger(paths)
    run(ledger)
    row = ledger.get_plan("release:orbit:v0.22.1")  # "…Z" in the file
    assert row.created_at == "2026-09-16T16:11:00.000+00:00"


def test_superseded_facts_are_kept_in_meta(paths):
    ledger = SqliteLedger(paths)
    run(ledger)
    row = ledger.get_plan("release:orbit:v0.22.1")
    assert row.meta == {
        "superseded_post_id": "1000000000000000003",
        "superseded_note": "originally landed on @otheraccount (wrong token); "
        "reposted to @constworks",
    }
    assert ledger.get_plan("pr:example:5").meta == {}


def test_second_run_is_all_already_present(paths):
    ledger = SqliteLedger(paths)
    run(ledger)
    before = [r.to_dict() for r in ledger.history(limit=50)]
    again = run(ledger)
    assert (again.imported_published, again.imported_skipped) == (0, 0)
    assert again.already_present == 7 and again.conflicts == [] and again.errors == []
    assert [r.to_dict() for r in ledger.history(limit=50)] == before


def test_imported_keys_replay_or_stay_skipped(paths):
    ledger = SqliteLedger(paths)
    run(ledger)

    def refuse(_usage) -> None:
        raise AssertionError("an imported key is not a new write")

    replay = claim(ledger, "pr:example:5", admit=refuse)
    assert replay.state == PUBLISHED and replay.post_id == "1000000000000000002"
    assert replay.url == url_for("1000000000000000002")
    skipped = claim(ledger, "pr:example:4", admit=refuse)
    assert skipped.state == SKIPPED and skipped.items == ()
    for key in ("pr:example:5", "pr:example:4"):
        with pytest.raises(PulsarError) as exc:
            claim(ledger, key, account=OTHER)
        assert exc.value.code == "idempotency_conflict"
    # A key the routine never recorded is a fresh write.
    assert claim(ledger, "pr:example:11").state == PENDING


def test_imports_cost_nothing_and_write_no_text_or_export(paths):
    ledger = SqliteLedger(paths, export=WriteLog(paths).export)
    run(ledger)
    usage = ledger.usage("x:constworks", day_start=DAY, month_start=MONTH)
    assert usage.spent_day_usd == 0 and usage.spent_month_usd == 0
    assert not paths.write_log.exists()  # historic rows are not pulsar's writes
    raw = paths.ledger_db.read_bytes()
    for wal in paths.home.glob("ledger.sqlite3-wal"):
        raw += wal.read_bytes()
    for line in lines():
        if "text" in line:
            assert line["text"].encode() not in raw


def test_key_taken_by_another_write_is_a_conflict(paths):
    ledger = SqliteLedger(paths)
    claim(ledger, "repo:example")
    report = run(ledger)
    assert report.imported_published == 3 and report.imported_skipped == 3
    ((line, key, reason),) = report.conflicts
    assert (line, key) == (3, "repo:example") and "publish" in reason
    row = ledger.get_plan("repo:example")
    assert row.tool == "publish" and row.state == PENDING  # left untouched


def test_imported_row_that_disagrees_with_its_line_is_a_conflict(paths, tmp_path):
    ledger = SqliteLedger(paths)
    run(ledger)
    changed = []
    for line in lines():
        if line["key"] == "pr:example:5":
            line = {**line, "post_id": "1000000000000000099"}
        if line["key"] == "pr:example:4":
            line = {**line, "note": "a different reason"}
        changed.append(json.dumps(line))
    path = tmp_path / "posted.jsonl"
    path.write_text("\n".join(changed) + "\n")
    report = run(ledger, path)
    assert report.already_present == 5
    reasons = {key: (number, reason) for number, key, reason in report.conflicts}
    assert reasons.keys() == {"pr:example:5", "pr:example:4"}
    assert reasons["pr:example:5"][0] == 5 and "post_id" in reasons["pr:example:5"][1]
    assert reasons["pr:example:4"][0] == 4 and "note" in reasons["pr:example:4"][1]
    assert ledger.get_plan("pr:example:5").post_id == "1000000000000000002"


def test_malformed_lines_are_reported_and_the_rest_imported(paths, tmp_path):
    good = FIXTURE.read_text().splitlines()
    bad = [
        "not json",
        "[1, 2]",
        json.dumps({"ts": "2026-09-13T01:08:12+00:00", "post_id": None}),
        json.dumps({"key": "has space", "ts": "2026-09-13T01:08:12+00:00", "post_id": None}),
        json.dumps({"key": "k-ts", "ts": "yesterday", "post_id": None}),
        json.dumps({"key": "k-naive", "ts": "2026-09-13T01:08:12", "post_id": None}),
        json.dumps({"key": "k-int", "ts": "2026-09-13T01:08:12Z", "post_id": 12}),
        json.dumps(
            {
                "key": "k-sec",
                "ts": "2026-09-13T01:08:12Z",
                "post_id": None,
                "note": "sk-" + "a" * 30,
            }
        ),
    ]
    path = tmp_path / "posted.jsonl"
    path.write_bytes(
        ("\n".join([good[0], *bad[:4], "", good[1], *bad[4:], *good[2:]]) + "\n").encode()
        + b'{"key": "\xff"}\n'
    )
    ledger = SqliteLedger(paths)
    report = run(ledger, path)
    assert (report.imported_published, report.imported_skipped) == (4, 3)
    assert [number for number, _ in report.errors] == [2, 3, 4, 5, 8, 9, 10, 11, 17]
    reasons = dict(report.errors)
    assert "not a JSON line" in reasons[2] and "not a JSON object" in reasons[3]
    assert "missing `key`" in reasons[4] and "bad `key`" in reasons[5]
    assert "ISO 8601" in reasons[8] and "timezone" in reasons[9]
    assert "post_id" in reasons[10] and "credential" in reasons[11]
    assert "sk-" not in reasons[11]
    assert ledger.get_plan("k-sec") is None and ledger.get_plan("k-ts") is None


def test_report_to_dict(paths, tmp_path):
    path = tmp_path / "posted.jsonl"
    path.write_text("oops\n")
    report = run(SqliteLedger(paths), path)
    assert report.to_dict() == {
        "applied": True,
        "imported_published": 0,
        "imported_skipped": 0,
        "already_present": 0,
        "conflicts": [],
        "errors": [{"line": 1, "reason": report.errors[0][1]}],
    }
