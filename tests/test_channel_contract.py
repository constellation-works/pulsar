"""The channel contract, run against every provider on its fake transport.

Each case drives a real channel (its client, its HTTP, its parsing) through
what the publisher, reconcile and the reader rely on: offline checks, whoami,
publishing posts, threads, replies, quotes and media with alt text, delete,
reconcile after a lost reply, and the two reads. A provider's own rules
(weighted length, graphemes and facets) are in its adapter's test file.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from pulsar.app.core.channels.bluesky import BlueskyChannel, BlueskyClient
from pulsar.app.core.channels.contract import Channel, Identity, PostSpec, Prices
from pulsar.app.core.channels.x import XChannel, XClient
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.core.publishing import Bound, Plan, Publisher
from pulsar.app.settings import Settings
from pulsar.internal.errors import PulsarError

from .conftest import FakeX, register
from .fake_bsky import ALICE, FakeBsky
from .media_samples import PNG

pytestmark = pytest.mark.anyio

PROVIDERS = ["x", "bsky"]
BSKY_ALIAS = "bsky:constworks.bsky.social"


@dataclass
class Sent:
    """One post as the provider received it."""

    text: str
    reply_to: str | None
    quote: str | None


@dataclass
class Harness:
    provider: str
    alias: str
    channel: Channel
    identity: Identity
    foreign: str  # someone else's post, to reply to and quote
    sent: Callable[[], list[Sent]]
    alts: Callable[[], list[str]]  # alt texts sent with media, in order
    exists: Callable[[str], bool]  # the post is on the account's timeline
    lose_next_reply: Callable[[], None]  # the next post is made, then its reply lost
    mention: Callable[[str], str]  # someone mentions the account; returns the post id
    like: Callable[[str, int], None]  # set a post's like count

    def bound(self) -> Bound:
        return Bound(
            alias=self.alias,
            provider=self.provider,
            user_id=self.identity.provider_user_id,
            handle=self.identity.handle,
            channel=self.channel,
        )


def _x_harness(store) -> Harness:
    fake = FakeX()
    timeline: list[dict[str, Any]] = []  # newest first
    mentions: list[dict[str, Any]] = []
    lose = [False]

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tweets") and request.method == "POST":
            resp = fake.handle(request)
            body = json.loads(request.content)
            refs = []
            if "reply" in body:
                refs.append({"type": "replied_to", "id": body["reply"]["in_reply_to_tweet_id"]})
            timeline.insert(
                0,
                {
                    "id": resp.json()["data"]["id"],
                    "text": body["text"],
                    "created_at": datetime.now(UTC).isoformat(),
                    "referenced_tweets": refs,
                    "public_metrics": {"like_count": 0},
                },
            )
            if lose[0]:
                lose[0] = False
                raise httpx.ReadTimeout("reply lost", request=request)
            return resp
        if path.endswith(f"/users/{fake.user_id}/tweets"):
            fake.requests.append(request)
            return httpx.Response(200, json={"data": timeline, "meta": {}})
        if path.endswith(f"/users/{fake.user_id}/mentions"):
            fake.requests.append(request)
            users = [{"id": "77", "username": "Alice"}]
            return httpx.Response(200, json={"data": mentions, "includes": {"users": users}})
        if "/tweets/" in path and request.method == "DELETE":
            post_id = path.rsplit("/", 1)[-1]
            timeline[:] = [t for t in timeline if t["id"] != post_id]
        return fake.handle(request)

    def sent() -> list[Sent]:
        out = []
        for r in fake.calls("POST", "/tweets"):
            body = json.loads(r.content)
            out.append(
                Sent(
                    text=body["text"],
                    reply_to=body.get("reply", {}).get("in_reply_to_tweet_id"),
                    quote=body.get("quote_tweet_id"),
                )
            )
        return out

    def alts() -> list[str]:
        return [
            json.loads(r.content)["metadata"]["alt_text"]["text"]
            for r in fake.calls("POST", "/media/metadata")
        ]

    def mention(text: str) -> str:
        post_id = str(1_800_000_000_000_000_000 + len(mentions))
        mentions.insert(
            0,
            {
                "id": post_id,
                "text": text,
                "author_id": "77",
                "created_at": datetime.now(UTC).isoformat(),
                "conversation_id": post_id,
                "public_metrics": {"like_count": 2},
            },
        )
        return post_id

    def like(post_id: str, n: int) -> None:
        next(t for t in timeline if t["id"] == post_id)["public_metrics"]["like_count"] = n

    channel = XChannel(
        XClient(store, transport=httpx.MockTransport(handle)),
        user_id=fake.user_id,
        handle=fake.username,
    )
    return Harness(
        provider="x",
        alias="x:constworks",
        channel=channel,
        identity=Identity(provider_user_id=fake.user_id, handle=fake.username),
        foreign="1790000000000000000",
        sent=sent,
        alts=alts,
        exists=lambda post_id: any(t["id"] == post_id for t in timeline),
        lose_next_reply=lambda: lose.__setitem__(0, True),
        mention=mention,
        like=like,
    )


def _bsky_harness(store) -> Harness:
    fake = FakeBsky()
    foreign = fake.add_other("3jzfcijpj2z2a", "a post by alice")

    def sent() -> list[Sent]:
        out = []
        for record in fake.posts():
            embed = record.get("embed", {})
            quoted = embed.get("record", {})
            quoted = quoted.get("record", quoted)  # recordWithMedia nests the record
            out.append(
                Sent(
                    text=record["text"],
                    reply_to=record.get("reply", {}).get("parent", {}).get("uri"),
                    quote=quoted.get("uri"),
                )
            )
        return out

    def alts() -> list[str]:
        found = []
        for record in fake.posts():
            embed = record.get("embed", {})
            media = embed.get("media", embed)
            found += [i["alt"] for i in media.get("images", [])]
        return found

    mentions = [0]

    def mention(text: str) -> str:
        mentions[0] += 1
        when = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return fake.mention(f"3kmention{mentions[0]}", text, created_at=when)

    def like(post_id: str, n: int) -> None:
        fake.counts.setdefault(post_id, {})["likeCount"] = n

    channel = BlueskyChannel(
        BlueskyClient(store, transport=fake.transport()), did=fake.did, handle=fake.handle
    )
    return Harness(
        provider="bsky",
        alias=BSKY_ALIAS,
        channel=channel,
        identity=Identity(provider_user_id=fake.did, handle=fake.handle),
        foreign=foreign,
        sent=sent,
        alts=alts,
        exists=lambda post_id: any(r["uri"] == post_id for r in fake.records.values()),
        lose_next_reply=lambda: setattr(fake, "create_raise_after_accept", httpx.ReadTimeout),
        mention=mention,
        like=like,
    )


@pytest.fixture(params=PROVIDERS)
async def harness(request, paths, bundle):
    if request.param == "x":
        h = _x_harness(register(paths, bundle))
    else:
        h = _bsky_harness(register(paths, bundle, alias=BSKY_ALIAS))
    yield h
    await h.channel.aclose()


@pytest.fixture
def publisher(paths, tmp_path) -> Publisher:
    root = tmp_path / "media"
    root.mkdir()
    (root / "chart.png").write_bytes(PNG)
    settings = Settings(media_roots=(root,))
    return Publisher(
        ledger=SqliteLedger(paths), settings=settings, deny=(paths.home,), media_base=root
    )


def _plan(*texts: str, media: bool = False, **extra: Any) -> Plan:
    posts = [{"text": t} for t in texts]
    if media:
        posts[0]["media"] = [{"path": "chart.png", "alt": "A chart of weekly posts"}]
    return Plan.from_mapping({"posts": posts, **extra})


# -- offline -------------------------------------------------------------------


async def test_capabilities_declare_the_whole_contract(harness):
    caps = harness.channel.capabilities
    assert caps.provider == harness.provider
    assert caps.threads and caps.reply and caps.quote and caps.delete
    assert caps.mentions and caps.metrics
    assert caps.media.alt_text and "image/png" in caps.media.mime_types
    assert caps.media.max_per_post == 4


async def test_check_post_holds_the_provider_length_limit(harness):
    channel, limit = harness.channel, harness.channel.capabilities.max_length
    check = channel.check_post(PostSpec(text="a" * limit), Prices(0.01, 0.5, 0.0))
    assert check.length == limit and not check.has_url and check.estimated_cost_usd == 0.01
    with pytest.raises(PulsarError) as exc:
        channel.check_post(PostSpec(text="a" * (limit + 1)), Prices())
    assert exc.value.code == "invalid_text"
    linked = channel.check_post(PostSpec(text="notes: https://example.com/n"), Prices(0.01, 0.5))
    assert linked.has_url and linked.estimated_cost_usd == 0.5
    with pytest.raises(PulsarError) as exc:
        channel.check_post(PostSpec(text="key ghp_" + "a1B2" * 9), Prices())
    assert exc.value.code == "secret_detected"


@pytest.mark.parametrize("bad", ["../users/1", "12ab", "at://did:plc:x/app.bsky.feed.like/1", ""])
async def test_check_target_refuses_what_is_not_a_post_id(harness, bad):
    harness.channel.check_target(reply_to=harness.foreign, quote=None)
    for target in ({"reply_to": bad, "quote": None}, {"reply_to": None, "quote": bad}):
        with pytest.raises(PulsarError) as exc:
            harness.channel.check_target(**target)
        assert exc.value.code == "invalid_argument"


async def test_the_fingerprint_matches_the_providers_copy(harness):
    published = await harness.channel.create("Fingerprint  me\nplease")
    since = datetime.now(UTC) - timedelta(minutes=5)
    recent = await harness.channel.recent_posts(since)
    assert recent.complete
    (found,) = [p for p in recent.posts if p.post_id == published.post_id]
    assert found.fingerprint == harness.channel.fingerprint("Fingerprint me please")


# -- network ---------------------------------------------------------------------


async def test_whoami_names_the_bound_account(harness):
    assert await harness.channel.whoami() == harness.identity


async def test_a_thread_with_media_publishes_as_a_reply_chain(harness, publisher):
    plan = _plan("first, with a chart", "second", media=True, reply_to=harness.foreign)
    prepared = publisher.prepare(plan, harness.bound())
    out = await publisher.publish(prepared, caller="contract")
    assert out.error is None and out.record.state == "published"
    first, second = (i.post_id for i in out.record.items)
    assert harness.sent() == [
        Sent(text="first, with a chart", reply_to=harness.foreign, quote=None),
        Sent(text="second", reply_to=first, quote=None),
    ]
    assert harness.alts() == ["A chart of weekly posts"]
    assert harness.exists(first) and harness.exists(second)
    row = publisher.ledger.get_plan(out.record.key)
    assert row is not None and row.provider == harness.provider


async def test_a_quote_publishes_with_media(harness, publisher):
    prepared = publisher.prepare(_plan("quoting", media=True, quote=harness.foreign),
                                 harness.bound())  # fmt: skip
    out = await publisher.publish(prepared, caller="contract")
    assert out.error is None
    assert harness.sent() == [Sent(text="quoting", reply_to=None, quote=harness.foreign)]
    assert harness.alts() == ["A chart of weekly posts"]


async def test_delete_removes_the_post_and_repeats_harmlessly(harness):
    published = await harness.channel.create("short-lived")
    assert harness.exists(published.post_id)
    assert await harness.channel.delete(published.post_id) is True
    assert not harness.exists(published.post_id)
    assert await harness.channel.delete(published.post_id) is True


async def test_reconcile_settles_a_post_whose_reply_was_lost(harness, publisher):
    prepared = publisher.prepare(_plan("did this go out?"), harness.bound())
    harness.lose_next_reply()
    out = await publisher.publish(prepared, caller="contract")
    assert out.error is not None and out.error.code == "outcome_unknown"
    assert out.record.state == "unknown"
    (result,) = await publisher.reconcile(harness.bound())
    assert result["state"] == "published" and result["error"] is None
    (item,) = publisher.ledger.get_plan(out.record.key).items
    assert harness.exists(item.post_id)
    assert len(harness.sent()) == 1, "reconcile never posts"


async def test_reads_return_mentions_and_own_posts_with_metrics(harness):
    since = datetime.now(UTC) - timedelta(hours=1)
    mention = harness.mention("hey @constworks, nice launch")
    mine = await harness.channel.create("our launch post")
    harness.like(mine.post_id, 7)

    mentions = await harness.channel.mentions(since, max_posts=10)
    assert mentions.complete and mentions.fetched >= 1
    (m,) = mentions.posts
    assert m.post_id == mention and m.author in ("alice", ALICE)
    assert m.text == "hey @constworks, nice launch"

    own = await harness.channel.own_posts(since, max_posts=10)
    (p,) = own.posts
    assert p.post_id == mine.post_id and p.text == "our launch post"
    assert p.metrics.likes == 7 and p.url == mine.url

    capped = await harness.channel.own_posts(since, max_posts=1)
    assert len(capped.posts) == 1
