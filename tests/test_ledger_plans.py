"""The plan API against ``SqliteLedger`` directly: claims, the item compare-and-set,
threads, skips, and a live sender racing reconcile."""

from __future__ import annotations

import json
import multiprocessing
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pulsar.app.core.ledger import (
    FAILED,
    PARTIAL,
    PENDING,
    PUBLISHED,
    RESOLVED_ABSENT,
    SKIPPED,
    SUBMITTING,
    UNKNOWN,
    SqliteLedger,
    Usage,
)
from pulsar.app.writelog import WriteLog
from pulsar.internal.errors import (
    IDEMPOTENCY_CONFLICT,
    INTERNAL,
    INVALID_ARGUMENT,
    OUTCOME_UNKNOWN,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.internal.fs import Paths

from .conftest import collect, reap
from .test_ledger import (
    ACCT,
    DAY,
    ME,
    MONTH,
    OTHER,
    Clock,
    claim_plan,
    intents,
    jsonl,
    send_all,
    sql,
    states,
)

# -- claim_plan -----------------------------------------------------------------------


def test_claim_plan_inserts_pending_row_and_items(paths):
    ledger = SqliteLedger(paths)
    seen: list[Usage] = []
    rec = claim_plan(ledger, n=3, admit=seen.append)
    assert seen == [Usage(0.0, 0.0, 0)]
    assert rec.state == PENDING and rec.attempts == 1 and rec.resume_from == 0
    assert rec.digest == "sha256:aaa" and rec.account_alias == "x:constworks"
    assert (rec.account_user_id, rec.account_handle, rec.provider) == (
        "1234567890", "constworks", "x"
    )  # fmt: skip
    assert states(rec) == [PENDING] * 3
    assert [i.fingerprint for i in rec.items] == ["fp0", "fp1", "fp2"]
    assert all(i.submitted_at is None for i in rec.items)
    json.dumps(rec.to_dict())  # serialisable for surfaces


@pytest.mark.parametrize(
    "change",
    [{"digest": "sha256:bbb"}, {"account": OTHER}, {"tool": "other_tool"}],
    ids=["digest", "account", "tool"],
)
def test_claim_plan_same_key_different_request_is_a_conflict(paths, change):
    ledger = SqliteLedger(paths)
    claim_plan(ledger)
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, **change)
    assert exc.value.code == IDEMPOTENCY_CONFLICT
    assert exc.value.detail == {"idempotency_key": "plan-1", "state": PENDING}


def test_claim_plan_key_of_a_legacy_write_is_a_conflict(paths):
    ledger = SqliteLedger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
    ledger.publish("k", post_id="1")
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, "k")
    assert exc.value.code == IDEMPOTENCY_CONFLICT


def test_claim_plan_refuses_bad_arguments_before_writing(paths):
    ledger = SqliteLedger(paths)
    for kwargs in ({"items": []}, {"provider": "mastodon"}):
        args = {
            "key": "bad",
            "tool": "publish",
            "digest": "d",
            "provider": "x",
            "account": ACCT,
            "caller": "t",
            "items": intents(1),
            "admit": None,
            "day_start": DAY,
            "month_start": MONTH,
        }
        with pytest.raises(PulsarError) as exc:
            ledger.claim_plan(**{**args, **kwargs})
        assert exc.value.code == INVALID_ARGUMENT
    with pytest.raises(PulsarError) as exc:
        ledger.claim_plan(
            key="bad", tool="publish", digest="d", provider="x", account=ACCT, caller="t",
            items=intents(1), admit=None, day_start=datetime(2026, 9, 26), month_start=MONTH,
        )  # fmt: skip
    assert exc.value.code == INVALID_ARGUMENT, "a naive window start is refused"
    assert ledger.get_plan("bad") is None


def test_claim_plan_published_replays_without_admission(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=2)
    done = send_all(ledger, "plan-1", 2)
    assert done.state == PUBLISHED

    def refuse(_: Usage) -> None:
        raise AssertionError("a replay is not a new write; policy must not run")

    again = claim_plan(ledger, n=2, admit=refuse)
    assert again.state == PUBLISHED and again.post_id == "500" and again.attempts == 1
    assert [i.post_id for i in again.items] == ["500", "501"]


@pytest.mark.parametrize("outcome", [UNKNOWN, SUBMITTING])
def test_claim_plan_blocks_on_unknown_or_in_flight(paths, outcome):
    ledger = SqliteLedger(paths)
    claim_plan(ledger)
    ledger.begin_item("plan-1", 0)
    if outcome == UNKNOWN:
        ledger.item_unknown("plan-1", 0, OutcomeUnknown("ReadTimeout"))
        ledger.finish("plan-1")
    with pytest.raises(OutcomeUnknown) as exc:
        claim_plan(ledger)
    assert exc.value.detail["idempotency_key"] == "plan-1"
    assert exc.value.detail["state"] == outcome
    assert exc.value.retryable is False


def test_claim_plan_pending_row_is_safe_to_reclaim(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=2)
    calls: list[Usage] = []
    again = claim_plan(ledger, n=2, admit=calls.append)
    assert again.state == PENDING and again.attempts == 1 and len(calls) == 1


def test_claim_plan_failed_row_is_rearmed(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=1)
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, PulsarError("api_error", "connect refused"))
    failed = ledger.finish("plan-1")
    assert failed.state == FAILED and failed.error_code == "api_error"
    again = claim_plan(ledger)
    assert again.state == PENDING and again.attempts == 2 and again.error_code is None
    (item,) = again.items
    assert item.state == PENDING and item.error_code is None and item.submitted_at is None


def test_admit_raising_writes_nothing(paths):
    ledger = SqliteLedger(paths)

    def over_budget(usage: Usage) -> None:
        raise PulsarError("budget_exceeded", "daily budget spent")

    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, n=3, admit=over_budget)
    assert exc.value.code == "budget_exceeded"
    assert ledger.get_plan("plan-1") is None and ledger.count() == 0
    assert sql(paths, "SELECT count(*) FROM items") == [(0,)]

    # A refused re-arm leaves the failed row as it was.
    claim_plan(ledger)
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, PulsarError("api_error", "down"))
    ledger.finish("plan-1")
    with pytest.raises(PulsarError):
        claim_plan(ledger, admit=over_budget)
    row = ledger.get_plan("plan-1")
    assert row.state == FAILED and row.attempts == 1 and states(row) == [FAILED]


# -- begin_item: the compare-and-set ------------------------------------------------


def test_begin_item_cas_two_ledgers_one_home(paths):
    a, b = SqliteLedger(paths), SqliteLedger(paths)
    assert claim_plan(a, n=2).state == PENDING
    assert claim_plan(b, n=2).state == PENDING  # nothing sent yet: both may hold it
    a.begin_item("plan-1", 0)
    with pytest.raises(OutcomeUnknown) as exc:
        b.begin_item("plan-1", 0)
    assert exc.value.detail == {
        "cause": exc.value.cause,
        "idempotency_key": "plan-1",
        "state": SUBMITTING,
        "idx": 0,
    }
    with pytest.raises(OutcomeUnknown):
        claim_plan(b, n=2)
    row = a.get_plan("plan-1")
    assert row.state == SUBMITTING and states(row) == [SUBMITTING, PENDING]


def test_begin_item_many_threads_admit_exactly_one(paths):
    claim_plan(SqliteLedger(paths))
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        ledger = SqliteLedger(paths)
        barrier.wait()
        try:
            ledger.begin_item("plan-1", 0)
            result = "started"
        except OutcomeUnknown:
            result = "blocked"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["blocked"] * (n - 1) + ["started"]


def _begin_in_process(home: str, go, results) -> None:
    ledger = SqliteLedger(Paths(Path(home)))
    go.wait(30)
    try:
        ledger.begin_item("plan-proc", 0)
        results.put("started")
    except OutcomeUnknown:
        results.put("blocked")


def test_begin_item_cas_across_processes(paths):
    claim_plan(SqliteLedger(paths), "plan-proc")
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_begin_in_process, args=(str(paths.home), go, results)) for _ in range(3)
    ]
    try:
        for p in procs:
            p.start()
        go.set()
        outcomes = sorted(collect(results, procs))
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0, f"a child exited {p.exitcode}"
    finally:
        reap(procs)
    assert outcomes == ["blocked", "blocked", "started"]


def test_begin_item_refuses_out_of_order_and_closed_rows(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=3)
    with pytest.raises(PulsarError) as exc:
        ledger.begin_item("plan-1", 1)
    assert exc.value.code == INVALID_ARGUMENT
    with pytest.raises(PulsarError) as exc:
        ledger.begin_item("plan-1", 3)
    assert exc.value.code == INVALID_ARGUMENT
    assert exc.value.detail == {"idempotency_key": "plan-1", "idx": 3}
    with pytest.raises(PulsarError) as exc:
        ledger.begin_item("nope", 0)
    assert exc.value.code == INTERNAL and exc.value.detail == {"idempotency_key": "nope"}
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, PulsarError("forbidden", "no"))
    ledger.finish("plan-1")  # failed: must be re-claimed before anything is sent
    with pytest.raises(OutcomeUnknown):
        ledger.begin_item("plan-1", 0)


# -- threads: partial, resume, unknown, reconcile -------------------------------------


def test_thread_failing_at_second_post_is_partial_and_resumes(paths):
    ledger = SqliteLedger(paths, export=WriteLog(paths).export)
    claim_plan(ledger, "thread", n=3)
    ledger.begin_item("thread", 0)
    ledger.item_published("thread", 0, post_id="500", url="https://x.com/constworks/status/500")
    ledger.begin_item("thread", 1)
    ledger.item_failed("thread", 1, PulsarError("duplicate", "duplicate content"))
    done = ledger.finish("thread")
    assert done.state == PARTIAL and states(done) == [PUBLISHED, FAILED, PENDING]
    assert done.post_id == "500" and done.error_code == "duplicate"
    assert done.resume_from == 1

    again = claim_plan(ledger, "thread", n=3)
    assert again.state == PENDING and again.attempts == 2
    assert states(again) == [PUBLISHED, PENDING, PENDING] and again.resume_from == 1
    assert again.items[0].post_id == "500"
    with pytest.raises(OutcomeUnknown):
        ledger.begin_item("thread", 0)  # already published: never re-sent
    final = send_all(ledger, "thread", 3, start=1)
    assert final.state == PUBLISHED and [i.post_id for i in final.items] == ["500", "501", "502"]
    assert [line["state"] for line in jsonl(paths)] == [PARTIAL, PUBLISHED]


def open_items(record):
    return {
        i.idx: (i.state, i.submitted_at) for i in record.items if i.state in (UNKNOWN, SUBMITTING)
    }


def test_unknown_blocks_reclaim_until_settled_absent(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, OutcomeUnknown("ReadTimeout"))  # ambiguous stays unknown
    rec = ledger.finish("plan-1")
    assert rec.state == UNKNOWN and states(rec) == [UNKNOWN, PENDING]
    assert rec.error_code == OUTCOME_UNKNOWN
    for _ in range(2):
        with pytest.raises(OutcomeUnknown):
            claim_plan(ledger, n=2)
    (listed,) = ledger.unresolved(stale_after=timedelta(hours=1), now=DAY)
    assert listed.key == "plan-1"
    seen = open_items(listed)
    with pytest.raises(PulsarError) as exc:  # pending: nothing to settle
        ledger.settle("plan-1", seen=seen, verdicts={1: None})
    assert exc.value.code == INVALID_ARGUMENT

    done = ledger.settle("plan-1", seen=seen, verdicts={0: None})
    assert done is not None and done.state == FAILED
    assert done.items[0].state == FAILED and done.items[0].error_code == RESOLVED_ABSENT
    assert ledger.unresolved(stale_after=timedelta(hours=1), now=DAY) == []
    again = claim_plan(ledger, n=2)
    assert again.state == PENDING and states(again) == [PENDING, PENDING]


def test_unknown_settled_as_published_resumes_after_it(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_unknown("plan-1", 0, OutcomeUnknown("HTTP 503"))
    listed = ledger.finish("plan-1")
    url = "https://x.com/constworks/status/900"
    done = ledger.settle("plan-1", seen=open_items(listed), verdicts={0: ("900", url)})
    assert done is not None and done.state == PARTIAL
    again = claim_plan(ledger, n=2)
    assert states(again) == [PUBLISHED, PENDING] and again.resume_from == 1


def test_stale_submitting_row_is_unresolved_and_finishes_unknown(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    t0 = clock.now
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_published("plan-1", 0, post_id="1", url="u1")
    clock.now = t0 + timedelta(minutes=2)
    ledger.begin_item("plan-1", 1)  # ...and the process dies here
    stale = timedelta(minutes=10)
    assert ledger.unresolved(stale_after=stale, now=t0 + timedelta(minutes=11)) == []
    (row,) = ledger.unresolved(stale_after=stale, now=t0 + timedelta(minutes=13))
    assert row.key == "plan-1" and row.state == SUBMITTING
    done = ledger.finish("plan-1")
    assert done.state == UNKNOWN and states(done) == [PUBLISHED, UNKNOWN]
    assert done.items[1].error_code == OUTCOME_UNKNOWN


def test_item_transitions_are_checked(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger)
    with pytest.raises(PulsarError) as exc:
        ledger.item_published("plan-1", 0, post_id="1", url="u")  # never begun
    assert exc.value.code == INTERNAL
    assert exc.value.detail == {"idempotency_key": "plan-1", "idx": 0, "state": PENDING}
    ledger.begin_item("plan-1", 0)
    ledger.item_published("plan-1", 0, post_id="1", url="u", media_ids=["710000"])
    assert ledger.get_plan("plan-1").items[0].media_ids == ("710000",)
    with pytest.raises(PulsarError) as exc:
        ledger.item_failed("plan-1", 0, PulsarError("api_error", "late"))
    assert exc.value.code == INTERNAL


def test_finishing_a_row_without_posts_is_an_internal_error(paths):
    ledger = SqliteLedger(paths)
    ledger.skip(key="s", provider="x", account=ACCT, caller="t", note=None)
    with pytest.raises(PulsarError) as exc:
        ledger.finish("s")
    assert exc.value.code == INTERNAL and exc.value.detail["state"] == SKIPPED


# -- skip ---------------------------------------------------------------------------


def test_skip_records_a_decision_never_to_post(paths):
    ledger = SqliteLedger(paths, export=WriteLog(paths).export)
    rec = ledger.skip(key="pr:4", provider="x", account=ACCT, caller="t", note="not a feature")
    assert rec.state == SKIPPED and rec.note == "not a feature" and rec.items == ()
    assert ledger.skip(key="pr:4", provider="x", account=ACCT, caller="t", note="again") == rec

    def refuse(_: Usage) -> None:
        raise AssertionError("a skipped key never reaches policy")

    got = claim_plan(ledger, "pr:4", admit=refuse)
    assert got.state == SKIPPED and got.items == ()
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, "pr:4", account=OTHER)
    assert exc.value.code == IDEMPOTENCY_CONFLICT
    (line,) = jsonl(paths)
    assert line["state"] == SKIPPED and line["idempotency_key"] == "pr:4"
    assert line["items"] == []


def test_skip_conflicts_with_published_or_in_flight(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger, "pub")
    send_all(ledger, "pub", 1)
    claim_plan(ledger, "flying")
    ledger.begin_item("flying", 0)
    for key in ("pub", "flying"):
        with pytest.raises(PulsarError) as exc:
            ledger.skip(key=key, provider="x", account=ACCT, caller="t", note=None)
        assert exc.value.code == IDEMPOTENCY_CONFLICT
    with pytest.raises(PulsarError) as exc:
        ledger.skip(key="pub", provider="x", account=OTHER, caller="t", note=None)
    assert exc.value.code == IDEMPOTENCY_CONFLICT
    secret_note = "token=abcdefghijklmnopqrstu"
    with pytest.raises(PulsarError) as exc:
        ledger.skip(key="s", provider="x", account=ACCT, caller="t", note=secret_note)
    assert exc.value.code == "secret_detected" and ledger.get_plan("s") is None


def test_skip_a_pending_row_stops_a_holder_from_sending(paths):
    ledger = SqliteLedger(paths)
    claim_plan(ledger)
    ledger.skip(key="plan-1", provider="x", account=ACCT, caller="t", note="changed our mind")
    with pytest.raises(OutcomeUnknown):
        ledger.begin_item("plan-1", 0)
    assert claim_plan(ledger).state == SKIPPED


# -- item_sending and settle: a live sender and reconcile never both win -----------


def test_item_sending_is_a_compare_and_set_on_the_senders_stamp(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    claim_plan(ledger, n=1)
    stamp = ledger.begin_item("plan-1", 0)
    assert ledger.item_sending("plan-1", 0, "2000-01-01T00:00:00.000+00:00") is None
    clock.now += timedelta(minutes=12)  # a long upload
    fresh = ledger.item_sending("plan-1", 0, stamp)
    assert fresh is not None and fresh > stamp
    assert ledger.get_plan("plan-1").items[0].submitted_at == fresh
    assert ledger.item_sending("plan-1", 0, stamp) is None, "the old stamp is spent"
    ledger.item_failed("plan-1", 0, PulsarError("forbidden", "no"))
    assert ledger.item_sending("plan-1", 0, fresh) is None


def test_settle_writes_nothing_when_the_row_changed_since_it_was_listed(paths):
    clock = Clock()
    ledger = SqliteLedger(paths, clock=clock)
    claim_plan(ledger, n=2)
    stamp = ledger.begin_item("plan-1", 0)
    seen = open_items(ledger.get_plan("plan-1"))
    clock.now += timedelta(seconds=1)
    ledger.item_sending("plan-1", 0, stamp)  # the sender is alive after all
    assert ledger.settle("plan-1", seen=seen, verdicts={0: None}) is None
    item = ledger.get_plan("plan-1").items[0]
    assert item.state == SUBMITTING and item.error_code is None

    now_seen = {0: (SUBMITTING, item.submitted_at)}
    settled = ledger.settle("plan-1", seen=now_seen, verdicts={0: None})
    assert settled is not None and settled.state == FAILED
    assert settled.items[0].error_code == RESOLVED_ABSENT
    assert settled.items[1].state == PENDING


def test_settle_records_a_found_post_and_finishes(paths):
    ledger = SqliteLedger(paths, clock=Clock(datetime(2026, 9, 26, 9, 0, tzinfo=UTC)))
    claim_plan(ledger, n=1)
    ledger.begin_item("plan-1", 0)
    ledger.item_unknown("plan-1", 0, OutcomeUnknown("ReadTimeout"))
    ledger.finish("plan-1")
    item = ledger.get_plan("plan-1").items[0]
    url = "https://x.com/constworks/status/900"
    done = ledger.settle(
        "plan-1", seen={0: (UNKNOWN, item.submitted_at)}, verdicts={0: ("900", url)}
    )
    assert done is not None and done.state == PUBLISHED and done.post_id == "900"
    assert ledger.settle("plan-1", seen={}, verdicts={}) is None, "a settled row is not open"
