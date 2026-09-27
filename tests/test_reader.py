"""Reads for engagement: budgeted before the call, recorded after it, never stored."""

from __future__ import annotations

from datetime import time, timedelta

import pytest

from pulsar.app.core.channels.contract import Mention, Metrics, OwnPost, Prices
from pulsar.app.core.engagement import Reader
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.core.publishing import PolicyConfig, day_window, month_window
from pulsar.app.settings import Settings
from pulsar.internal.errors import PulsarError

from .media_samples import PNG
from .test_ledger import sql
from .test_publisher import NOW, FakeChannel, bound_to, make_publisher, thread

pytestmark = pytest.mark.anyio

SINCE = NOW - timedelta(hours=24)


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def media_root(tmp_path):
    root = tmp_path / "media"
    root.mkdir()
    (root / "a.png").write_bytes(PNG)
    return root


def mention(post_id: str, author: str = "alice", *, minutes: int = 60) -> Mention:
    return Mention(
        post_id=post_id,
        url=f"https://x.com/{author}/status/{post_id}",
        author=author,
        text=f"hi from {author}",
        created_at=NOW - timedelta(minutes=minutes),
        conversation_id=None,
        reply_to=None,
        metrics=Metrics(),
    )


def make_reader(paths, clock, **policy) -> Reader:
    settings = Settings(
        provider_prices=(("fake", Prices(plain_post_usd=0.01, read_post_usd=0.01)),),
        policy=PolicyConfig(**policy),
    )
    return Reader(
        ledger=SqliteLedger(paths, clock=lambda: clock[0]), settings=settings, now=lambda: clock[0]
    )


def spent_today(reader: Reader, alias: str = "fake:acct") -> float:
    tz = reader.settings.policy.tz
    usage = reader.ledger.usage(
        alias, day_start=day_window(NOW, tz)[0], month_start=month_window(NOW, tz)[0]
    )
    return usage.spent_day_usd


async def test_mentions_are_recorded_by_count_and_cost_not_content(paths, clock):
    reader = make_reader(paths, clock)
    channel = FakeChannel(clock=clock, mentioned=[mention("801"), mention("802", "bob")])
    read = await reader.mentions(bound_to(channel), since=SINCE, max_posts=20, caller="engager")
    assert [m.post_id for m in read.posts] == ["801", "802"]
    assert read.complete and read.cost_usd == 0.02 and read.replied == frozenset()
    assert channel.read_calls == [("mentions", SINCE, 20)]
    rows = sql(
        paths, "SELECT kind, account_alias, caller, posts, est_cost_usd, complete FROM reads"
    )
    assert rows == [("mentions", "fake:acct", "engager", 2, 0.02, 1)]
    assert b"hi from" not in paths.ledger_db.read_bytes(), "the ledger keeps no read text"
    assert spent_today(reader) == 0.02, "reads spend from the same budget as posts"


async def test_the_accounts_own_posts_are_not_mentions(paths, clock):
    reader = make_reader(paths, clock)
    channel = FakeChannel(clock=clock, mentioned=[mention("801"), mention("802", "acct")])
    read = await reader.mentions(bound_to(channel), since=SINCE, max_posts=20, caller="t")
    assert [m.post_id for m in read.posts] == ["801"]
    assert read.cost_usd == 0.02, "both were returned, so both are billed"


async def test_a_mention_the_account_replied_to_is_marked(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    channel = FakeChannel(clock=clock, mentioned=[mention("801"), mention("802")])
    bound = bound_to(channel)
    out = await pub.publish(pub.prepare(thread("thanks!", reply_to="801"), bound), caller="t")
    assert out.error is None
    reader = make_reader(paths, clock)
    read = await reader.mentions(bound, since=SINCE, max_posts=20, caller="t")
    assert read.replied == frozenset({"801"})
    other = await reader.mentions(
        bound_to(channel, alias="fake:other"), since=SINCE, max_posts=20, caller="t"
    )
    assert other.replied == frozenset(), "another account's reply answers nothing here"


async def test_a_failed_reply_does_not_answer_the_mention(paths, clock, media_root):
    pub = make_publisher(paths, clock, media_root)
    channel = FakeChannel(clock=clock, fail_on={0: "fail"}, mentioned=[mention("801")])
    bound = bound_to(channel)
    out = await pub.publish(pub.prepare(thread("thanks!", reply_to="801"), bound), caller="t")
    assert out.record.state == "failed"
    read = await make_reader(paths, clock).mentions(bound, since=SINCE, max_posts=5, caller="t")
    assert read.replied == frozenset()


async def test_a_read_over_budget_is_refused_before_the_call(paths, clock):
    reader = make_reader(paths, clock, daily_budget_usd=0.05)
    channel = FakeChannel(clock=clock, mentioned=[mention("801")])
    with pytest.raises(PulsarError) as exc:
        await reader.mentions(bound_to(channel), since=SINCE, max_posts=10, caller="t")
    assert exc.value.code == "budget_exceeded" and exc.value.detail["planned_usd"] == 0.1
    assert channel.read_calls == [] and sql(paths, "SELECT count(*) FROM reads") == [(0,)]


async def test_quiet_hours_and_the_post_cap_do_not_stop_a_read(paths, clock):
    reader = make_reader(paths, clock, quiet_hours=(time(0), time(23, 59)), max_posts_per_day=0)
    channel = FakeChannel(clock=clock, own=[])
    read = await reader.own_posts(bound_to(channel), since=SINCE, max_posts=5, caller="t")
    assert read.posts == () and read.cost_usd == 0.0


async def test_a_failed_read_records_nothing(paths, clock):
    reader = make_reader(paths, clock)
    channel = FakeChannel(clock=clock, read_error=PulsarError("rate_limited", "slow down"))
    with pytest.raises(PulsarError):
        await reader.mentions(bound_to(channel), since=SINCE, max_posts=5, caller="t")
    assert sql(paths, "SELECT count(*) FROM reads") == [(0,)]


async def test_read_spend_blocks_a_post_that_would_overspend(paths, clock, media_root):
    reader = make_reader(paths, clock, daily_budget_usd=0.03)
    channel = FakeChannel(clock=clock, mentioned=[mention(str(800 + i)) for i in range(3)])
    await reader.mentions(bound_to(channel), since=SINCE, max_posts=3, caller="t")
    pub = make_publisher(paths, clock, media_root, daily_budget_usd=0.03)
    with pytest.raises(PulsarError) as exc:
        await pub.publish(pub.prepare(thread("one more"), bound_to(channel)), caller="t")
    assert exc.value.code == "budget_exceeded"


async def test_own_posts_carry_their_metrics(paths, clock):
    post = OwnPost(
        post_id="900",
        url="u/900",
        text="shipped",
        created_at=NOW - timedelta(hours=1),
        reply_to=None,
        metrics=Metrics(likes=4, impressions=120),
    )
    reader = make_reader(paths, clock)
    read = await reader.own_posts(
        bound_to(FakeChannel(clock=clock, own=[post])), since=SINCE, max_posts=5, caller="t"
    )
    assert read.posts == (post,) and read.cost_usd == 0.01
    assert sql(paths, "SELECT kind FROM reads") == [("own_posts",)]


async def test_a_credential_shaped_caller_is_refused(paths, clock):
    reader = make_reader(paths, clock)
    with pytest.raises(PulsarError) as exc:
        await reader.mentions(
            bound_to(FakeChannel(clock=clock)),
            since=SINCE,
            max_posts=5,
            caller="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789",
        )
    assert exc.value.code == "secret_detected"
