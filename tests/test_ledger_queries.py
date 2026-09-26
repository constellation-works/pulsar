"""Reads over the ledger: usage for policy, history, counts, and the jsonl export."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from pulsar.core.errors import OutcomeUnknown, PulsarError
from pulsar.core.ledger import PUBLISHED, Ledger
from pulsar.core.usage import Usage
from pulsar.core.writelog import WriteLog

from .test_ledger import (
    ACCT,
    DAY,
    MONTH,
    OTHER,
    TEXTS,
    Clock,
    claim_plan,
    jsonl,
    send_all,
    sha,
)


def test_usage_windows(paths):
    clock = Clock(datetime(2026, 9, 25, 10, 0, tzinfo=UTC))  # yesterday: month only
    ledger = Ledger(paths, clock=clock)
    claim_plan(ledger, "yesterday", n=2, cost=0.2)
    send_all(ledger, "yesterday", 2)

    clock.now = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    claim_plan(ledger, "published", cost=0.015)
    send_all(ledger, "published", 1)
    claim_plan(ledger, "unknown", account=OTHER, cost=0.2)
    ledger.begin_item("unknown", 0)
    ledger.item_unknown("unknown", 0, OutcomeUnknown("ReadTimeout"))
    claim_plan(ledger, "in-flight", account=OTHER, cost=0.1)
    ledger.begin_item("in-flight", 0)
    claim_plan(ledger, "failed", n=2, cost=0.5)  # failed, the rest of a closed row, skipped: free
    ledger.begin_item("failed", 0)
    ledger.item_failed("failed", 0, PulsarError("forbidden", "no"))
    ledger.finish("failed")
    claim_plan(ledger, "pending", n=3, cost=0.7)  # claimed, not sent yet: reserved
    ledger.skip(key="skipped", provider="x", account=ACCT, caller="t", note=None)

    mine = ledger.usage("x:constworks", day_start=DAY, month_start=MONTH)
    assert mine == Usage(spent_day_usd=2.415, spent_month_usd=2.815, posts_day=4)
    theirs = ledger.usage("x:someone", day_start=DAY, month_start=MONTH)
    assert theirs == Usage(spent_day_usd=2.415, spent_month_usd=2.815, posts_day=2)
    # Window starts in another zone compare as instants: 12:00+02:00 is 10:00Z,
    # after this morning's 09:00Z posts and claims.
    later = datetime(2026, 9, 26, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ledger.usage("x:constworks", day_start=later, month_start=MONTH) == Usage(0.0, 2.815, 0)
    # admit sees the same numbers, computed inside the claim's transaction.
    seen: list[Usage] = []
    claim_plan(ledger, "next", admit=seen.append)
    assert seen == [mine]


def test_history_newest_first_and_per_account(paths):
    clock = Clock()
    ledger = Ledger(paths, clock=clock)
    for i, account in enumerate([ACCT, OTHER, ACCT]):
        clock.now = DAY + timedelta(minutes=i)
        claim_plan(ledger, f"k{i}", account=account)
    assert [r.key for r in ledger.history()] == ["k2", "k1", "k0"]
    assert [r.key for r in ledger.history(limit=1)] == ["k2"]
    assert [r.key for r in ledger.history(account_alias="x:constworks")] == ["k2", "k0"]


def test_count_is_the_total_history_matches_without_its_limit(paths):
    ledger = Ledger(paths)
    assert ledger.count() == 0
    for i, account in enumerate([ACCT, OTHER, ACCT]):
        claim_plan(ledger, f"k{i}", account=account)
    assert len(ledger.history(limit=1)) == 1 and ledger.count() == 3
    assert ledger.count(account_alias="x:constworks") == 2
    assert ledger.count(account_alias="x:nobody") == 0


def test_last_published_filters_before_the_limit(paths):
    """60 newer unpublished rows do not hide the published one."""
    clock = Clock(DAY)
    ledger = Ledger(paths, clock=clock)
    assert ledger.last_published("x:constworks") is None
    claim_plan(ledger, "old-post")
    send_all(ledger, "old-post", 1)
    clock.now += timedelta(minutes=1)
    claim_plan(ledger, "theirs", account=OTHER)  # another account's newer post
    send_all(ledger, "theirs", 1, first_post=900)
    for i in range(60):
        clock.now += timedelta(minutes=1)
        claim_plan(ledger, f"pending-{i}")
    assert all(r.state != PUBLISHED for r in ledger.history(limit=50, account_alias=ACCT.alias))

    last = ledger.last_published("x:constworks")
    assert last is not None and last.key == "old-post" and last.post_id == "500"
    assert [i.post_id for i in last.items] == ["500"]
    theirs = ledger.last_published("x:someone")
    assert theirs is not None and theirs.key == "theirs"


def test_plan_export_lines_carry_ids_and_hashes_never_text(paths):
    ledger = Ledger(paths, export=WriteLog(paths).export)
    claim_plan(ledger, "thread", n=3)
    send_all(ledger, "thread", 3)
    (line,) = jsonl(paths)
    assert line["dry_run"] is False and line["tool"] == "publish" and line["caller"] == "tester"
    assert line["state"] == PUBLISHED and line["idempotency_key"] == "thread"
    assert line["post_id"] == "500" and line["account_user_id"] == "1234567890"
    assert line["text_sha256"] == sha(TEXTS[0])
    assert line["account_alias"] == "x:constworks" and line["plan_digest"] == "sha256:aaa"
    assert line["items"] == [
        {"idx": 0, "state": PUBLISHED, "post_id": "500"},
        {"idx": 1, "state": PUBLISHED, "post_id": "501"},
        {"idx": 2, "state": PUBLISHED, "post_id": "502"},
    ]
    raw = paths.write_log.read_bytes() + paths.ledger_db.read_bytes()
    for wal in paths.home.glob("ledger.sqlite3-wal"):
        raw += wal.read_bytes()
    assert not any(text.encode() in raw for text in TEXTS)


def test_failed_export_does_not_fail_finish(paths):
    def broken(_record) -> None:
        raise OSError("disk full")

    ledger = Ledger(paths, export=broken)
    claim_plan(ledger)
    assert send_all(ledger, "plan-1", 1).state == PUBLISHED
