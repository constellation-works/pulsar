"""The write ledger through the legacy MCP tools: idempotency, outcome
classification, persistence and export as a tool contract.

The storage properties are tested against ``SqliteLedger`` directly in the
``test_ledger_*`` modules, which share the helpers at the end of this one.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import sqlite3
import stat
from datetime import UTC, datetime

import anyio
import httpx
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from pulsar.core.errors import OUTCOME_UNKNOWN
from pulsar.core.ledger import (
    FAILED,
    PUBLISHED,
    SCHEMA_VERSION,
    SUBMITTING,
    UNKNOWN,
    AccountRef,
    ItemIntent,
    SqliteLedger,
    request_digest,
)
from pulsar.core.ledger.schema import SCHEMA_V1
from pulsar.mcp import Runtime, build_server

from .conftest import SECRETS, make_runtime
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
    runtime = make_runtime(paths, transport=flaky_x.transport())
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
        "replayed": False,
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
    assert other["replayed"] is False and len(flaky_x.posts()) == 2


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
    assert ok["ok"] is True and ok["replayed"] is False
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
    ledger = SqliteLedger(paths)
    digest = request_digest(
        "create_post", text="lost", reply_to_post_id=None, quote_post_id=None, media_ids=[]
    )
    ledger.claim(key="k-crash", tool="create_post", digest=digest, account=ME, caller="a")
    rt = make_runtime(paths, transport=flaky_x.transport())
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
        rt = make_runtime(paths, transport=flaky_x.transport())
        try:
            async with open_session(rt) as s:
                out = await call(s, "create_post", args)
        finally:
            await rt.aclose()
        assert out["ok"] is True and out.get("replayed", False) is expected_replay
    assert len(flaky_x.posts()) == 1


async def test_two_runtimes_on_one_home_cannot_both_post(paths, authed, flaky_x):
    flaky_x.tweet_gate = anyio.Event()
    arrived = anyio.Event()  # rt1's POST reached X: its row is claimed and in flight
    handle = flaky_x.handle_async

    async def spy(request):
        if request.method == "POST" and request.url.path.endswith("/tweets"):
            arrived.set()
        return await handle(request)

    flaky_x.handle_async = spy  # before the transports bind it
    rt1 = make_runtime(paths, transport=flaky_x.transport())
    rt2 = make_runtime(paths, transport=flaky_x.transport())
    args = {"text": "only once", "idempotency_key": "k-race"}
    results: dict[str, dict] = {}
    try:
        async with open_session(rt1) as s1, open_session(rt2) as s2:

            async def first() -> None:
                results["first"] = await call(s1, "create_post", args)

            async with anyio.create_task_group() as tg:
                tg.start_soon(first)
                with anyio.fail_after(5):
                    await arrived.wait()
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


# -- delete and upload ------------------------------------------------------------


async def test_repeated_delete_replays_the_receipt(session, authed, flaky_x, rt, paths):
    first = await call(session, "delete_post", {"post_id": "101"})
    second = await call(session, "delete_post", {"post_id": "101"})
    assert first == {
        "ok": True,
        "account": "x:constworks",
        "post_id": "101",
        "deleted": True,
        "replayed": False,
    }
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


async def test_a_failing_settle_write_never_replaces_the_tool_error(
    session, authed, flaky_x, rt, monkeypatch
):
    await call(session, "whoami", {})
    flaky_x.fail_auth_once = True
    flaky_x.refresh_status = 401

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(rt.ledger, "fail", locked)
    out = await call(session, "delete_post", {"post_id": "203"})
    assert out["code"] == "auth_expired", "the send's error, not the ledger's"


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
    ok_row, bad_row = (rt.ledger.get(r.key) for r in reversed(rt.ledger.history()))
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
        assert "not identity" in props["caller"]["description"], (
            "caller is self-asserted; the schema must not let agents read it as auth"
        )
    for name in ("create_post", "delete_post"):
        props = by_name[name].input_schema["properties"]
        assert "replayed" in props["idempotency_key"]["description"], (
            "agents must learn that re-sending a key replays, not reposts (a paid duplicate)"
        )


async def test_jsonl_export_keeps_legacy_fields(session, authed, paths, monkeypatch):
    monkeypatch.setenv("PULSAR_CALLER", "grokbot")
    await call(session, "create_post", {"text": "first light", "idempotency_key": "k-exp"})
    line = jsonl(paths)[-1]
    legacy = {"ts", "tool", "caller", "dry_run", "post_id", "text_sha256"}
    assert legacy <= line.keys()
    assert line["dry_run"] is False and line["caller"] == "grokbot"
    assert line["state"] == PUBLISHED and line["idempotency_key"] == "k-exp"
    assert "first light" not in json.dumps(line)


# == shared by the test_ledger_* modules ============================================

ACCT = AccountRef(alias="x:constworks", provider="x", user_id="1234567890", handle="constworks")
OTHER = AccountRef(alias="x:someone", provider="x", user_id="999", handle="someone")
DAY = datetime(2026, 9, 26, tzinfo=UTC)
MONTH = datetime(2026, 9, 1, tzinfo=UTC)
TEXTS = ["first of three", "second of three", "third of three"]


class Clock:
    """A settable clock for ``SqliteLedger(clock=...)``: assign or advance ``now``."""

    def __init__(self, now: datetime = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


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
    conn.executescript(SCHEMA_V1)
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


def sql(paths, query: str, *args):
    """Rows straight from the file, bypassing ``SqliteLedger``."""
    conn = sqlite3.connect(paths.ledger_db)
    try:
        return conn.execute(query, args).fetchall()
    finally:
        conn.close()
