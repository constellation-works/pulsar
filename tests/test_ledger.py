"""The write ledger: idempotency, outcome classification, persistence, export.

Driven through a real MCP session where the behaviour is a tool contract,
and against ``Ledger`` directly for the storage properties.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import multiprocessing
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

import pulsar.core.ledger as ledger_mod
from pulsar.core.errors import OUTCOME_UNKNOWN, OutcomeUnknown, PulsarError
from pulsar.core.ledger import (
    _SCHEMA_V1,
    FAILED,
    PARTIAL,
    PENDING,
    PUBLISHED,
    RESOLVED_ABSENT,
    SCHEMA_VERSION,
    SKIPPED,
    SUBMITTING,
    UNKNOWN,
    AccountRef,
    ItemIntent,
    Ledger,
    request_digest,
)
from pulsar.core.paths import Paths
from pulsar.core.usage import Usage
from pulsar.core.writelog import WriteLog
from pulsar.surfaces.mcp import Runtime, build_server

from .conftest import SECRETS
from .media_samples import MP4

pytestmark = pytest.mark.anyio

ME = {"user_id": "1234567890", "username": "constworks"}


@contextlib.asynccontextmanager
async def open_session(rt: Runtime):
    server = build_server(rt)
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s


@pytest.fixture
async def rt(paths, flaky_x):
    runtime = Runtime(paths, transport=flaky_x.transport())
    yield runtime
    await runtime.aclose()


@pytest.fixture
async def session(rt):
    async with open_session(rt) as s:
        yield s


async def call(session, tool, args):
    result = await session.call_tool(tool, args)
    assert not result.is_error, result
    out = result.structured_content
    if out is None:
        out = json.loads(result.content[0].text)
    blob = json.dumps(out)
    assert not any(s in blob for s in SECRETS)
    return out


def jsonl(paths):
    if not paths.write_log.exists():
        return []
    return [json.loads(line) for line in paths.write_log.read_text().splitlines()]


# -- replay and conflict ------------------------------------------------------


async def test_same_key_twice_posts_once_and_replays(session, authed, flaky_x, rt):
    args = {"text": "first light", "idempotency_key": "launch-2026-09-26"}
    first = await call(session, "create_post", args)
    second = await call(session, "create_post", args)
    assert len(flaky_x.posts()) == 1
    assert first == {
        "ok": True,
        "post_id": "101",
        "url": "https://x.com/constworks/status/101",
        "text": "first light",
    }
    assert second == {**first, "replayed": True}
    row = rt.ledger.get("launch-2026-09-26")
    assert row.state == PUBLISHED and row.post_id == "101" and row.attempts == 1
    assert row.text_sha256 == hashlib.sha256(b"first light").hexdigest()


async def test_default_key_dedupes_identical_requests(session, authed, flaky_x):
    await call(session, "create_post", {"text": "same words"})
    again = await call(session, "create_post", {"text": "same words"})
    assert again["replayed"] is True and len(flaky_x.posts()) == 1
    other = await call(session, "create_post", {"text": "same words", "reply_to_post_id": "9"})
    assert "replayed" not in other and len(flaky_x.posts()) == 2


async def test_same_key_different_text_is_a_conflict(session, authed, flaky_x):
    await call(session, "create_post", {"text": "v1", "idempotency_key": "k1"})
    out = await call(session, "create_post", {"text": "v2", "idempotency_key": "k1"})
    assert out["ok"] is False and out["code"] == "idempotency_conflict"
    assert out["retryable"] is False
    assert out["detail"] == {"idempotency_key": "k1", "state": PUBLISHED}
    assert len(flaky_x.posts()) == 1


async def test_key_reused_across_tools_is_a_conflict(session, authed, flaky_x):
    await call(session, "create_post", {"text": "v1", "idempotency_key": "k1"})
    out = await call(session, "delete_post", {"post_id": "101", "idempotency_key": "k1"})
    assert out["code"] == "idempotency_conflict"
    assert flaky_x.calls("DELETE") == []


@pytest.mark.parametrize("key", ["", "has space", "tab\there", "x" * 201, "zw​j"])
async def test_bad_idempotency_key_is_rejected_before_network(session, authed, flaky_x, key):
    out = await call(session, "create_post", {"text": "hi", "idempotency_key": key})
    assert out["code"] == "invalid_argument"
    assert flaky_x.requests == []


async def test_secret_looking_key_is_rejected(session, authed, flaky_x, paths):
    key = "sk-abcdefghijklmnopqrstuvwxyz"
    out = await call(session, "create_post", {"text": "hi", "idempotency_key": key})
    assert out["code"] == "secret_detected" and key not in json.dumps(out)
    assert flaky_x.requests == [] and not paths.ledger_db.exists()


# -- outcome classification ---------------------------------------------------


async def test_read_timeout_after_send_is_outcome_unknown_and_never_reposted(
    session, authed, flaky_x, rt, paths
):
    flaky_x.tweet_raise = httpx.ReadTimeout
    args = {"text": "did it go?", "idempotency_key": "k-timeout"}
    out = await call(session, "create_post", args)
    assert out["ok"] is False and out["code"] == OUTCOME_UNKNOWN
    assert out["retryable"] is False
    assert "Do NOT retry" in out["message"]
    assert out["detail"]["idempotency_key"] == "k-timeout"
    assert out["detail"]["cause"].startswith("ReadTimeout")
    assert len(flaky_x.posts()) == 1  # X did create it
    row = rt.ledger.get("k-timeout")
    assert row.state == UNKNOWN and row.error_code == OUTCOME_UNKNOWN and row.post_id is None

    again = await call(session, "create_post", args)
    assert again["code"] == OUTCOME_UNKNOWN and again["detail"]["state"] == UNKNOWN
    assert len(flaky_x.posts()) == 1, "an unknown outcome must never be re-sent"

    line = jsonl(paths)[-1]
    assert line["state"] == UNKNOWN and line["idempotency_key"] == "k-timeout"
    assert line["error_code"] == OUTCOME_UNKNOWN and line["post_id"] is None


async def test_connect_error_is_a_retryable_failure_and_retry_posts(
    session, authed, flaky_x, rt, paths
):
    flaky_x.tweet_raise = httpx.ConnectError
    flaky_x.raise_after_accept = False
    args = {"text": "try again", "idempotency_key": "k-connect"}
    out = await call(session, "create_post", args)
    assert out["code"] == "api_error" and out["retryable"] is True
    assert flaky_x.posts() == []
    assert rt.ledger.get("k-connect").state == FAILED

    ok = await call(session, "create_post", args)
    assert ok["ok"] is True and "replayed" not in ok
    assert len(flaky_x.posts()) == 1
    row = rt.ledger.get("k-connect")
    assert row.state == PUBLISHED and row.attempts == 2 and row.error_code is None
    assert [line["state"] for line in jsonl(paths)] == [FAILED, PUBLISHED]


@pytest.mark.parametrize("status", [500, 503])
async def test_5xx_on_post_is_outcome_unknown(session, authed, flaky_x, rt, status):
    flaky_x.tweet_status = status
    out = await call(session, "create_post", {"text": "hmm", "idempotency_key": "k5"})
    assert out["code"] == OUTCOME_UNKNOWN and out["detail"]["status"] == status
    assert rt.ledger.get("k5").state == UNKNOWN


async def test_unparseable_2xx_is_outcome_unknown(session, authed, flaky_x, rt):
    flaky_x.tweet_raw = b"<html>ok</html>"
    out = await call(session, "create_post", {"text": "hmm", "idempotency_key": "k2xx"})
    assert out["code"] == OUTCOME_UNKNOWN
    assert rt.ledger.get("k2xx").state == UNKNOWN


async def test_duplicate_is_a_definitive_failure(session, authed, flaky_x, rt, paths):
    flaky_x.tweet_status = 403
    flaky_x.tweet_body = {"detail": "You are not allowed to create a Tweet with duplicate content."}
    out = await call(session, "create_post", {"text": "again", "idempotency_key": "kdup"})
    assert out["code"] == "duplicate" and out["retryable"] is False
    row = rt.ledger.get("kdup")
    assert row.state == FAILED and row.error_code == "duplicate" and row.retryable is False
    assert jsonl(paths)[-1]["error_code"] == "duplicate"


async def test_401_refresh_retry_still_posts_once(session, authed, flaky_x, rt):
    flaky_x.fail_auth_once = True
    out = await call(session, "create_post", {"text": "after refresh", "idempotency_key": "k401"})
    assert out["ok"] is True
    assert len(flaky_x.calls("POST", "/oauth2/token")) == 1
    assert rt.ledger.get("k401").state == PUBLISHED


async def test_unexpected_exception_mid_post_is_outcome_unknown(session, authed, rt, monkeypatch):
    async def boom(*_, **__):
        raise RuntimeError("bug")

    monkeypatch.setattr(rt.client, "create_post", boom)
    out = await call(session, "create_post", {"text": "oops", "idempotency_key": "kbug"})
    assert out["code"] == OUTCOME_UNKNOWN
    assert rt.ledger.get("kbug").state == UNKNOWN


async def test_crash_mid_post_leaves_a_submitting_row(paths, authed, flaky_x):
    """A process that dies after claiming leaves evidence, and blocks a re-post."""
    ledger = Ledger(paths)
    digest = request_digest(
        "create_post", text="lost", reply_to_post_id=None, quote_post_id=None, media_ids=[]
    )
    ledger.claim(key="k-crash", tool="create_post", digest=digest, account=ME, caller="a")
    rt = Runtime(paths, transport=flaky_x.transport())
    try:
        async with open_session(rt) as s:
            out = await call(s, "create_post", {"text": "lost", "idempotency_key": "k-crash"})
    finally:
        await rt.aclose()
    assert out["code"] == OUTCOME_UNKNOWN and out["detail"]["state"] == SUBMITTING
    assert flaky_x.posts() == []


# -- dry run, persistence, concurrency ------------------------------------------


async def test_dry_run_writes_neither_ledger_nor_jsonl(session, authed, flaky_x, paths):
    out = await call(session, "create_post", {"text": "just checking", "dry_run": True})
    assert out["dry_run"] is True and flaky_x.requests == []
    assert not paths.ledger_db.exists() and not paths.write_log.exists()


async def test_ledger_file_is_private_wal_and_versioned(session, authed, paths):
    await call(session, "create_post", {"text": "hello"})
    assert stat.S_IMODE(paths.ledger_db.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.home.stat().st_mode) == 0o700
    for sidecar in paths.home.glob("ledger.sqlite3-*"):
        assert stat.S_IMODE(sidecar.stat().st_mode) & 0o077 == 0, sidecar
    conn = sqlite3.connect(paths.ledger_db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    finally:
        conn.close()


async def test_ledger_survives_a_new_runtime(paths, authed, flaky_x):
    args = {"text": "persist me", "idempotency_key": "k-persist"}
    for expected_replay in (False, True):
        rt = Runtime(paths, transport=flaky_x.transport())
        try:
            async with open_session(rt) as s:
                out = await call(s, "create_post", args)
        finally:
            await rt.aclose()
        assert out["ok"] is True and out.get("replayed", False) is expected_replay
    assert len(flaky_x.posts()) == 1


async def test_two_runtimes_on_one_home_cannot_both_post(paths, authed, flaky_x):
    flaky_x.tweet_gate = anyio.Event()
    rt1 = Runtime(paths, transport=flaky_x.transport())
    rt2 = Runtime(paths, transport=flaky_x.transport())
    args = {"text": "only once", "idempotency_key": "k-race"}
    results: dict[str, dict] = {}
    try:
        async with open_session(rt1) as s1, open_session(rt2) as s2:

            async def first() -> None:
                results["first"] = await call(s1, "create_post", args)

            async with anyio.create_task_group() as tg:
                tg.start_soon(first)
                with anyio.fail_after(5):
                    while rt2.ledger.get("k-race") is None:
                        await anyio.sleep(0.01)
                # rt1 holds the key and its POST is in flight.
                results["second"] = await call(s2, "create_post", args)
                flaky_x.tweet_gate.set()
            results["third"] = await call(s2, "create_post", args)
    finally:
        await rt1.aclose()
        await rt2.aclose()
    assert results["second"]["code"] == OUTCOME_UNKNOWN
    assert results["second"]["detail"]["state"] == SUBMITTING
    assert results["first"]["ok"] is True
    assert results["third"] == {**results["first"], "replayed": True}
    assert len(flaky_x.posts()) == 1


def test_concurrent_claims_from_many_processes_admit_exactly_one(paths):
    """Separate Ledger objects stand in for processes; each opens its own connection."""
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        ledger = Ledger(paths)
        barrier.wait()
        try:
            rec = ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
            result = rec.state
        except OutcomeUnknown:
            result = "blocked"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["blocked"] * (n - 1) + [SUBMITTING]


def test_key_from_another_account_is_a_conflict(paths):
    ledger = Ledger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
    ledger.publish("k", post_id="1")
    other = {"user_id": "999", "username": "someone"}
    with pytest.raises(PulsarError) as exc:
        ledger.claim(key="k", tool="create_post", digest="d", account=other, caller="t")
    assert exc.value.code == "idempotency_conflict"


# -- delete and upload ------------------------------------------------------------


async def test_repeated_delete_replays_the_receipt(session, authed, flaky_x, rt, paths):
    first = await call(session, "delete_post", {"post_id": "101"})
    second = await call(session, "delete_post", {"post_id": "101"})
    assert first == {"ok": True, "post_id": "101", "deleted": True}
    assert second == {**first, "replayed": True}
    assert len(flaky_x.calls("DELETE")) == 1
    row = rt.ledger.get("delete:101")
    assert row.state == PUBLISHED and row.meta == {"deleted": True}
    line = jsonl(paths)[-1]
    assert line["tool"] == "delete_post" and line["idempotency_key"] == "delete:101"


async def test_failed_delete_is_recorded_and_retryable(session, authed, flaky_x, rt):
    await call(session, "whoami", {})  # cache the account before auth breaks
    flaky_x.fail_auth_once = True
    flaky_x.refresh_status = 401
    out = await call(session, "delete_post", {"post_id": "202"})
    assert out["code"] == "auth_expired"
    row = rt.ledger.get("delete:202")
    assert row.state == FAILED and row.error_code == "auth_expired"
    flaky_x.refresh_status = 200
    ok = await call(session, "delete_post", {"post_id": "202"})
    assert ok["ok"] is True and rt.ledger.get("delete:202").attempts == 2


async def test_upload_rows_record_media_facts_not_bytes(session, authed, flaky_x, rt, paths):
    data = MP4 + b"distinct-video-payload"  # real ftyp header: content is sniffed
    up = await call(
        session, "upload_media", {"base64": base64.b64encode(data).decode(), "mime": "video/mp4"}
    )
    assert up["ok"] is True
    flaky_x.media_finalize_info = {"state": "failed", "error": {"message": "codec"}}
    bad = await call(
        session, "upload_media", {"base64": base64.b64encode(data).decode(), "mime": "video/mp4"}
    )
    assert bad["code"] == "invalid_media"
    ok_row, bad_row = rt.ledger.all()
    assert ok_row.tool == "upload_media" and ok_row.state == PUBLISHED
    assert ok_row.media_id == "710000"
    assert ok_row.meta == {"mime": "video/mp4", "bytes": len(data), "processing_state": "succeeded"}
    assert bad_row.state == FAILED and bad_row.media_id is None
    assert bad_row.meta["processing_state"] == "failed"
    assert ok_row.idempotency_key != bad_row.idempotency_key
    raw = paths.ledger_db.read_bytes() + paths.write_log.read_bytes()
    assert data not in raw
    for wal in paths.home.glob("ledger.sqlite3-wal"):
        assert data not in wal.read_bytes()


async def test_schemas_mark_caller_advisory_and_document_the_key(session):
    by_name = {t.name: t for t in (await session.list_tools()).tools}
    for name in ("create_post", "upload_media", "delete_post"):
        props = by_name[name].input_schema["properties"]
        assert "not identity" in props["caller"]["description"]
    for name in ("create_post", "delete_post"):
        props = by_name[name].input_schema["properties"]
        assert "replayed" in props["idempotency_key"]["description"]
    assert "outcome_unknown" in by_name["create_post"].description


async def test_jsonl_export_keeps_legacy_fields(session, authed, paths, monkeypatch):
    monkeypatch.setenv("PULSAR_CALLER", "grokbot")
    await call(session, "create_post", {"text": "first light", "idempotency_key": "k-exp"})
    line = jsonl(paths)[-1]
    legacy = {"ts", "tool", "caller", "dry_run", "post_id", "text_sha256"}
    assert legacy <= line.keys()
    assert line["dry_run"] is False and line["caller"] == "grokbot"
    assert line["state"] == PUBLISHED and line["idempotency_key"] == "k-exp"
    assert "first light" not in json.dumps(line)


# == schema v2: plans, threads, usage =============================================

ACCT = AccountRef(alias="x:constworks", provider="x", user_id="1234567890", handle="constworks")
OTHER = AccountRef(alias="x:someone", provider="x", user_id="999", handle="someone")
DAY = datetime(2026, 9, 26, tzinfo=UTC)
MONTH = datetime(2026, 9, 1, tzinfo=UTC)
TEXTS = ["first of three", "second of three", "third of three"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def intents(n: int, cost: float = 0.015) -> list[ItemIntent]:
    return [ItemIntent(sha(TEXTS[i % 3]), f"fp{i}", cost) for i in range(n)]


def claim_plan(
    ledger,
    key="plan-1",
    *,
    digest="sha256:aaa",
    n=1,
    account=ACCT,
    admit=None,
    tool="publish",
    cost=0.015,
):
    return ledger.claim_plan(
        key=key,
        tool=tool,
        digest=digest,
        provider=account.provider,
        account=account,
        caller="tester",
        items=intents(n, cost),
        admit=admit,
        day_start=DAY,
        month_start=MONTH,
    )


def send_all(ledger, key, n, *, start=0, first_post=500):
    for idx in range(start, n):
        ledger.begin_item(key, idx)
        pid = str(first_post + idx)
        ledger.item_published(key, idx, post_id=pid, url=f"https://x.com/constworks/status/{pid}")
    return ledger.finish(key)


def states(record):
    return [i.state for i in record.items]


@pytest.fixture
def clock(monkeypatch):
    """The ledger's clock, settable: ``clock[0] = datetime(...)``."""
    now = [datetime(2026, 9, 26, 12, 0, tzinfo=UTC)]
    monkeypatch.setattr(ledger_mod, "_now", lambda: ledger_mod._iso(now[0]))
    return now


# -- migration ----------------------------------------------------------------------

V1_COLUMNS = (
    "idempotency_key, tool, account_user_id, account_handle, caller, request_digest,"
    " text_sha256, state, post_id, media_id, url, error_code, error_message, retryable,"
    " meta_json, attempts, created_at, updated_at"
)
T0 = "2026-09-20T10:00:00.000+00:00"
T1 = "2026-09-20T10:00:05.000+00:00"


def build_v1_ledger(paths) -> None:
    """A database exactly as the v1 code path left it, with one row per shape."""
    paths.ensure()
    conn = sqlite3.connect(paths.ledger_db)
    conn.executescript(_SCHEMA_V1)
    conn.execute("PRAGMA user_version = 1")
    rows = [
        ("k-pub", "create_post", "1234567890", "ConstWorks", "a", "d-pub", sha("hello"),
         "published", "101", None, "https://x.com/constworks/status/101", None, None, None,
         "{}", 1, T0, T1),
        ("k-unk", "create_post", "1234567890", "ConstWorks", "a", "d-unk", sha("lost"),
         "unknown", None, None, None, OUTCOME_UNKNOWN, "ReadTimeout", 0, "{}", 1, T0, T1),
        ("k-fail", "create_post", "1234567890", "ConstWorks", "a", "d-fail", sha("dup"),
         "failed", None, None, None, "duplicate", "dup", 0, "{}", 2, T0, T1),
        ("delete:9", "delete_post", "1234567890", "ConstWorks", "a", "d-del", None,
         "published", "9", None, None, None, None, None, '{"deleted": true}', 1, T0, T1),
        ("upload:u1", "upload_media", "1234567890", "ConstWorks", "a", "d-up", None,
         "published", None, "710000", None, None, None, None,
         '{"bytes": 3, "mime": "image/png"}', 1, T0, T1),
    ]  # fmt: skip
    conn.executemany(f"INSERT INTO writes ({V1_COLUMNS}) VALUES ({', '.join('?' * 18)})", rows)
    conn.commit()
    conn.close()


def test_v1_database_migrates_in_place(paths):
    build_v1_ledger(paths)
    ledger = Ledger(paths)
    before = {"k-pub", "k-unk", "k-fail", "delete:9", "upload:u1"}
    assert {r.idempotency_key for r in ledger.all()} == before

    conn = sqlite3.connect(paths.ledger_db)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert {"writes_state", "writes_post_id", "writes_account", "writes_alias"} <= names
    assert {"items", "items_state", "items_submitted"} <= names
    assert "writes_v2" not in names

    # v1 readers see the same rows.
    pub = ledger.get("k-pub")
    assert pub.state == PUBLISHED and pub.post_id == "101" and pub.created_at == T0
    assert ledger.get("upload:u1").meta == {"bytes": 3, "mime": "image/png"}
    assert ledger.get("k-fail").attempts == 2

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
    ledger = Ledger(paths)
    claim_plan(ledger, "after-migration")
    conn = sqlite3.connect(paths.ledger_db)
    try:
        ids = [r[0] for r in conn.execute("SELECT id FROM writes ORDER BY id")]
        orphans = conn.execute(
            "SELECT count(*) FROM items WHERE write_id NOT IN (SELECT id FROM writes)"
        ).fetchone()[0]
    finally:
        conn.close()
    assert ids == [1, 2, 3, 4, 5, 6] and orphans == 0


def test_newer_schema_is_refused(paths):
    Ledger(paths).get("x")
    conn = sqlite3.connect(paths.ledger_db)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(PulsarError) as exc:
        Ledger(paths).get("x")
    assert exc.value.code == "invalid_config"


def test_legacy_create_post_rows_mirror_one_item(paths):
    ledger = Ledger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t", text_sha256="h")
    plan = ledger.get_plan("k")
    assert plan.account_alias == "x:constworks" and states(plan) == [SUBMITTING]
    ledger.fail("k", PulsarError("api_error", "down"))
    assert ledger.get_plan("k").items[0].error_code == "api_error"
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t", text_sha256="h")
    ledger.publish("k", post_id="77", url="https://x.com/constworks/status/77")
    (item,) = ledger.get_plan("k").items
    assert (item.state, item.post_id, item.error_code) == (PUBLISHED, "77", None)
    ledger.claim(key="up", tool="upload_media", digest="u", account=ME, caller="t")
    assert ledger.get_plan("up").items == ()


# -- claim_plan -----------------------------------------------------------------------


def test_claim_plan_inserts_pending_row_and_items(paths):
    ledger = Ledger(paths)
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
    ledger = Ledger(paths)
    claim_plan(ledger)
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, **change)
    assert exc.value.code == "idempotency_conflict"
    assert exc.value.detail == {"idempotency_key": "plan-1", "state": PENDING}


def test_claim_plan_key_of_a_legacy_write_is_a_conflict(paths):
    ledger = Ledger(paths)
    ledger.claim(key="k", tool="create_post", digest="d", account=ME, caller="t")
    ledger.publish("k", post_id="1")
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, "k")
    assert exc.value.code == "idempotency_conflict"


def test_claim_plan_published_replays_without_admission(paths):
    ledger = Ledger(paths)
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
    ledger = Ledger(paths)
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
    ledger = Ledger(paths)
    claim_plan(ledger, n=2)
    calls: list[Usage] = []
    again = claim_plan(ledger, n=2, admit=calls.append)
    assert again.state == PENDING and again.attempts == 1 and len(calls) == 1


def test_claim_plan_failed_row_is_rearmed(paths):
    ledger = Ledger(paths)
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
    ledger = Ledger(paths)

    def over_budget(usage: Usage) -> None:
        raise PulsarError("budget_exceeded", "daily budget spent")

    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, n=3, admit=over_budget)
    assert exc.value.code == "budget_exceeded"
    assert ledger.get_plan("plan-1") is None and ledger.all() == []
    conn = sqlite3.connect(paths.ledger_db)
    try:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0
    finally:
        conn.close()

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
    a, b = Ledger(paths), Ledger(paths)
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
    claim_plan(Ledger(paths))
    n = 8
    barrier = threading.Barrier(n)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        ledger = Ledger(paths)
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
    ledger = Ledger(Paths(Path(home)))
    go.wait(30)
    try:
        ledger.begin_item("plan-proc", 0)
        results.put("started")
    except OutcomeUnknown:
        results.put("blocked")


def test_begin_item_cas_across_processes(paths):
    claim_plan(Ledger(paths), "plan-proc")
    ctx = multiprocessing.get_context("spawn")
    go = ctx.Event()
    results = ctx.Queue()
    procs = [
        ctx.Process(target=_begin_in_process, args=(str(paths.home), go, results)) for _ in range(3)
    ]
    for p in procs:
        p.start()
    go.set()
    outcomes = sorted(results.get(timeout=60) for _ in procs)
    for p in procs:
        p.join(timeout=30)
        assert p.exitcode == 0
    assert outcomes == ["blocked", "blocked", "started"]


def test_begin_item_refuses_out_of_order_and_closed_rows(paths):
    ledger = Ledger(paths)
    claim_plan(ledger, n=3)
    with pytest.raises(PulsarError) as exc:
        ledger.begin_item("plan-1", 1)
    assert exc.value.code == "invalid_argument"
    with pytest.raises(ValueError):
        ledger.begin_item("plan-1", 3)
    with pytest.raises(KeyError):
        ledger.begin_item("nope", 0)
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, PulsarError("forbidden", "no"))
    ledger.finish("plan-1")  # failed: must be re-claimed before anything is sent
    with pytest.raises(OutcomeUnknown):
        ledger.begin_item("plan-1", 0)


# -- threads: partial, resume, unknown, reconcile -------------------------------------


def test_thread_failing_at_second_post_is_partial_and_resumes(paths):
    ledger = Ledger(paths, export=WriteLog(paths).export)
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


def test_unknown_blocks_reclaim_until_resolved_absent(paths):
    ledger = Ledger(paths)
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_failed("plan-1", 0, OutcomeUnknown("ReadTimeout"))  # ambiguous stays unknown
    rec = ledger.finish("plan-1")
    assert rec.state == UNKNOWN and states(rec) == [UNKNOWN, PENDING]
    assert rec.error_code == OUTCOME_UNKNOWN
    for _ in range(2):
        with pytest.raises(OutcomeUnknown):
            claim_plan(ledger, n=2)
    unresolved = ledger.unresolved(stale_after=timedelta(hours=1), now=DAY)
    assert [r.key for r in unresolved] == ["plan-1"]
    with pytest.raises(ValueError):
        ledger.resolve_item("plan-1", 1, post_id=None, url=None)  # pending: nothing to resolve

    ledger.resolve_item("plan-1", 0, post_id=None, url=None)
    item = ledger.get_plan("plan-1").items[0]
    assert item.state == FAILED and item.error_code == RESOLVED_ABSENT
    assert ledger.finish("plan-1").state == FAILED
    assert ledger.unresolved(stale_after=timedelta(hours=1), now=DAY) == []
    again = claim_plan(ledger, n=2)
    assert again.state == PENDING and states(again) == [PENDING, PENDING]


def test_unknown_resolved_as_published_resumes_after_it(paths):
    ledger = Ledger(paths)
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_unknown("plan-1", 0, OutcomeUnknown("HTTP 503"))
    ledger.finish("plan-1")
    ledger.resolve_item("plan-1", 0, post_id="900", url="https://x.com/constworks/status/900")
    assert ledger.finish("plan-1").state == PARTIAL
    again = claim_plan(ledger, n=2)
    assert states(again) == [PUBLISHED, PENDING] and again.resume_from == 1


def test_stale_submitting_row_is_unresolved_and_finishes_unknown(paths, clock):
    ledger = Ledger(paths)
    t0 = clock[0]
    claim_plan(ledger, n=2)
    ledger.begin_item("plan-1", 0)
    ledger.item_published("plan-1", 0, post_id="1", url="u1")
    clock[0] = t0 + timedelta(minutes=2)
    ledger.begin_item("plan-1", 1)  # ...and the process dies here
    stale = timedelta(minutes=10)
    assert ledger.unresolved(stale_after=stale, now=t0 + timedelta(minutes=11)) == []
    (row,) = ledger.unresolved(stale_after=stale, now=t0 + timedelta(minutes=13))
    assert row.key == "plan-1" and row.state == SUBMITTING
    done = ledger.finish("plan-1")
    assert done.state == UNKNOWN and states(done) == [PUBLISHED, UNKNOWN]
    assert done.items[1].error_code == OUTCOME_UNKNOWN


def test_item_transitions_are_checked(paths):
    ledger = Ledger(paths)
    claim_plan(ledger)
    with pytest.raises(ValueError):
        ledger.item_published("plan-1", 0, post_id="1", url="u")  # never begun
    ledger.begin_item("plan-1", 0)
    ledger.item_published("plan-1", 0, post_id="1", url="u", media_ids=["710000"])
    assert ledger.get_plan("plan-1").items[0].media_ids == ("710000",)
    with pytest.raises(ValueError):
        ledger.item_failed("plan-1", 0, PulsarError("api_error", "late"))


# -- skip ---------------------------------------------------------------------------


def test_skip_records_a_decision_never_to_post(paths):
    ledger = Ledger(paths, export=WriteLog(paths).export)
    rec = ledger.skip(key="pr:4", provider="x", account=ACCT, caller="t", note="not a feature")
    assert rec.state == SKIPPED and rec.note == "not a feature" and rec.items == ()
    assert ledger.skip(key="pr:4", provider="x", account=ACCT, caller="t", note="again") == rec

    def refuse(_: Usage) -> None:
        raise AssertionError("a skipped key never reaches policy")

    got = claim_plan(ledger, "pr:4", admit=refuse)
    assert got.state == SKIPPED and got.items == ()
    with pytest.raises(PulsarError) as exc:
        claim_plan(ledger, "pr:4", account=OTHER)
    assert exc.value.code == "idempotency_conflict"
    (line,) = jsonl(paths)
    assert line["state"] == SKIPPED and line["idempotency_key"] == "pr:4"
    assert line["items"] == []


def test_skip_conflicts_with_published_or_in_flight(paths):
    ledger = Ledger(paths)
    claim_plan(ledger, "pub")
    send_all(ledger, "pub", 1)
    claim_plan(ledger, "flying")
    ledger.begin_item("flying", 0)
    for key in ("pub", "flying"):
        with pytest.raises(PulsarError) as exc:
            ledger.skip(key=key, provider="x", account=ACCT, caller="t", note=None)
        assert exc.value.code == "idempotency_conflict"
    with pytest.raises(PulsarError) as exc:
        ledger.skip(key="pub", provider="x", account=OTHER, caller="t", note=None)
    assert exc.value.code == "idempotency_conflict"
    secret_note = "token=abcdefghijklmnopqrstu"
    with pytest.raises(PulsarError) as exc:
        ledger.skip(key="s", provider="x", account=ACCT, caller="t", note=secret_note)
    assert exc.value.code == "secret_detected" and ledger.get_plan("s") is None


def test_skip_a_pending_row_stops_a_holder_from_sending(paths):
    ledger = Ledger(paths)
    claim_plan(ledger)
    ledger.skip(key="plan-1", provider="x", account=ACCT, caller="t", note="changed our mind")
    with pytest.raises(OutcomeUnknown):
        ledger.begin_item("plan-1", 0)
    assert claim_plan(ledger).state == SKIPPED


# -- usage --------------------------------------------------------------------------


def test_usage_windows(paths, clock):
    ledger = Ledger(paths)
    clock[0] = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)  # yesterday: month only
    claim_plan(ledger, "yesterday", n=2, cost=0.2)
    send_all(ledger, "yesterday", 2)

    clock[0] = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
    claim_plan(ledger, "published", cost=0.015)
    send_all(ledger, "published", 1)
    claim_plan(ledger, "unknown", account=OTHER, cost=0.2)
    ledger.begin_item("unknown", 0)
    ledger.item_unknown("unknown", 0, OutcomeUnknown("ReadTimeout"))
    claim_plan(ledger, "in-flight", account=OTHER, cost=0.1)
    ledger.begin_item("in-flight", 0)
    claim_plan(ledger, "failed", cost=0.5)  # failed, pending and skipped are free
    ledger.begin_item("failed", 0)
    ledger.item_failed("failed", 0, PulsarError("forbidden", "no"))
    ledger.finish("failed")
    claim_plan(ledger, "pending", n=3, cost=0.7)
    ledger.skip(key="skipped", provider="x", account=ACCT, caller="t", note=None)

    mine = ledger.usage("x:constworks", day_start=DAY, month_start=MONTH)
    assert mine == Usage(spent_day_usd=0.315, spent_month_usd=0.715, posts_day=1)
    theirs = ledger.usage("x:someone", day_start=DAY, month_start=MONTH)
    assert theirs == Usage(spent_day_usd=0.315, spent_month_usd=0.715, posts_day=2)
    # Window starts in another zone compare as instants: 12:00+02:00 is 10:00Z,
    # after this morning's 09:00Z posts.
    later = datetime(2026, 9, 26, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert ledger.usage("x:constworks", day_start=later, month_start=MONTH) == Usage(0.0, 0.715, 0)
    # admit sees the same numbers, computed inside the claim's transaction.
    seen: list[Usage] = []
    claim_plan(ledger, "next", admit=seen.append)
    assert seen == [mine]


# -- history and export -------------------------------------------------------------


def test_history_newest_first_and_per_account(paths, clock):
    ledger = Ledger(paths)
    for i, account in enumerate([ACCT, OTHER, ACCT]):
        clock[0] = DAY + timedelta(minutes=i)
        claim_plan(ledger, f"k{i}", account=account)
    assert [r.key for r in ledger.history()] == ["k2", "k1", "k0"]
    assert [r.key for r in ledger.history(limit=1)] == ["k2"]
    assert [r.key for r in ledger.history(account_alias="x:constworks")] == ["k2", "k0"]


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
