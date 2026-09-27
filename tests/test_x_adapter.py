import json
from datetime import UTC, datetime

import httpx
import pytest

from pulsar.app.core.channels.contract import LoadedMedia, MediaRef, PostSpec, Prices
from pulsar.app.core.channels.x import XChannel, XClient, fingerprint, remote_fingerprint
from pulsar.internal.errors import PulsarError

from .media_samples import JPEG, MP4
from .media_samples import PNG as PNG_1PX

pytestmark = pytest.mark.anyio

SINCE = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)


def _media(mime: str, data: bytes = PNG_1PX, alt: str = "a chart") -> LoadedMedia:
    return LoadedMedia(data=data, mime=mime, alt=alt, sha256="0" * 64)


@pytest.fixture
async def channel(store, authed, fake_x):
    timeline: list[dict] = []
    mentions: list[dict] = []
    users = [{"id": "77", "username": "Alice"}, {"id": "1234567890", "username": "constworks"}]

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media/metadata"):
            fake_x.requests.append(request)
            return httpx.Response(200, json={"data": {"associated_metadata": True}})
        if "/users/1234567890/mentions" in request.url.path:
            fake_x.requests.append(request)
            size = int(request.url.params["max_results"])
            start = int(request.url.params.get("pagination_token") or 0)
            chunk = mentions[start : start + size]
            more = len(mentions) > start + size
            return httpx.Response(
                200,
                json={
                    "data": chunk,
                    "includes": {"users": users},
                    "meta": {"next_token": str(start + size)} if more else {},
                },
            )
        if "/users/1234567890/tweets" in request.url.path:
            fake_x.requests.append(request)
            token = request.url.params.get("pagination_token")
            page = int(token) if token else 0
            chunk = timeline[page * 2 : page * 2 + 2]
            meta = {"next_token": str(page + 1)} if len(timeline) > (page + 1) * 2 else {}
            return httpx.Response(200, json={"data": chunk, "meta": meta})
        return fake_x.handle(request)

    client = XClient(store, transport=httpx.MockTransport(handle))
    ch = XChannel(client, user_id="1234567890", handle="constworks")
    ch.timeline = timeline  # type: ignore[attr-defined]
    ch.mentioned = mentions  # type: ignore[attr-defined]
    yield ch
    await ch.aclose()


def test_fingerprint_survives_x_rewriting():
    local = "Orbit v0.25 & friends <3 — notes: https://github.com/constellation-works/orbit"
    as_x_shows_it = (
        "Orbit v0.25 &amp; friends &lt;3 — notes: https://t.co/AbC123 https://t.co/media9"
    )
    assert fingerprint(local) == remote_fingerprint(as_x_shows_it)
    assert fingerprint("Café  two\n\nlines") == fingerprint("Café two lines")
    assert fingerprint("Orbit v0.25") != fingerprint("Orbit v0.26")


def test_fingerprint_unescapes_only_what_x_escaped():
    # A literal "&amp;" in the post comes back from X as "&amp;amp;".
    local = "Write &amp; in HTML"
    assert fingerprint(local) == remote_fingerprint("Write &amp;amp; in HTML")
    assert fingerprint(local) != fingerprint("Write & in HTML")


def test_fingerprint_ignores_the_mentions_x_puts_in_front_of_a_reply():
    assert fingerprint("thanks, merged") == remote_fingerprint("@alice @bob_2 thanks, merged")
    assert fingerprint("@alice thanks") == remote_fingerprint("@alice @alice thanks")
    # Only a leading run: a mention inside the text is part of the text.
    assert fingerprint("thanks @alice") != fingerprint("thanks")
    assert fingerprint("@alice") == remote_fingerprint("@alice"), "a lone mention is kept"


async def test_check_post_uses_x_weighted_length_and_prices(channel):
    check = channel.check_post(PostSpec(text="see https://example.com"), Prices(0.01, 0.5))
    assert check.has_url and check.estimated_cost_usd == 0.5 and check.length == 4 + 23
    with pytest.raises(PulsarError) as exc:
        channel.check_post(PostSpec(text="x" * 281), Prices())
    assert exc.value.code == "invalid_text"
    five = tuple(MediaRef(path=f"{i}.png", alt="a") for i in range(5))
    with pytest.raises(PulsarError) as exc:
        channel.check_post(PostSpec(text="hi", media=five), Prices())
    assert exc.value.code == "invalid_media"


@pytest.mark.parametrize(
    ("mimes", "ok"),
    [
        (["image/png"] * 4, True),
        (["image/png", "image/jpeg"], True),
        (["video/mp4"], True),
        (["image/gif"], True),
        (["video/mp4", "image/png"], False),
        (["image/gif", "image/png"], False),
        (["image/png"] * 5, False),
    ],
)
async def test_check_media_mixing_rules(channel, mimes, ok):
    media = tuple(_media(m) for m in mimes)
    if ok:
        channel.check_media(media)
    else:
        with pytest.raises(PulsarError):
            channel.check_media(media)


async def test_alt_text_limit(channel):
    with pytest.raises(PulsarError) as exc:
        channel.check_media((_media("image/png", alt="a" * 1001),))
    assert "1000" in exc.value.message


async def test_check_target_requires_numeric_ids(channel):
    channel.check_target(reply_to="123", quote=None)
    with pytest.raises(PulsarError) as exc:
        channel.check_target(reply_to=None, quote="../1")
    assert exc.value.code == "invalid_argument"


async def test_whoami_is_an_identity(channel):
    me = await channel.whoami()
    assert (me.provider_user_id, me.handle) == ("1234567890", "constworks")


async def test_image_upload_sets_alt_text_video_does_not(channel, fake_x):
    await channel.upload(_media("image/jpeg", JPEG, alt="The release banner"))
    meta = fake_x.calls("POST", "/media/metadata")
    assert len(meta) == 1
    assert json.loads(meta[0].content) == {
        "id": "710000",
        "metadata": {"alt_text": {"text": "The release banner"}},
    }
    fake_x.requests.clear()
    await channel.upload(_media("video/mp4", MP4, alt="A demo video"))
    assert fake_x.calls("POST", "/media/metadata") == []


async def test_create_returns_the_account_url(channel, fake_x):
    out = await channel.create("hello", reply_to="99", media_ids=("710000",))
    assert out.url == f"https://x.com/constworks/status/{out.post_id}"
    body = json.loads(fake_x.calls("POST", "/tweets")[0].content)
    assert body["reply"] == {"in_reply_to_tweet_id": "99"}
    assert body["media"] == {"media_ids": ["710000"]}


async def test_recent_posts_paginates_and_reports_completeness(channel, fake_x, monkeypatch):
    posts = [
        {"id": str(900 + i), "text": f"post {i}", "created_at": f"2026-09-26T01:0{i}:00.000Z"}
        for i in range(5)
    ]
    channel.timeline.extend(posts)
    recent = await channel.recent_posts(SINCE)
    assert recent.complete and [p.post_id for p in recent.posts] == [p["id"] for p in posts]
    assert recent.posts[0].fingerprint == fingerprint("post 0")
    assert recent.posts[0].created_at == datetime(2026, 9, 26, 1, 0, tzinfo=UTC)
    first = fake_x.calls("GET")[0]
    assert first.url.params["start_time"] == "2026-09-26T01:00:00Z"

    monkeypatch.setattr("pulsar.app.core.channels.x.adapter.RECONCILE_MAX_PAGES", 2)
    capped = await channel.recent_posts(SINCE)
    assert not capped.complete and len(capped.posts) == 4


@pytest.mark.parametrize(
    "unreadable",
    [
        {"id": "950", "text": "no time"},
        {"id": "951", "text": "bad time", "created_at": "yesterday"},
        {"text": "no id", "created_at": "2026-09-26T01:05:00.000Z"},
    ],
)
async def test_an_unreadable_post_makes_the_listing_incomplete(channel, unreadable):
    """A post that cannot be placed in time is neither invented nor dropped silently:
    the listing says it is incomplete, so reconcile never reads it as absence."""
    good = {"id": "900", "text": "fine", "created_at": "2026-09-26T01:00:00.000Z"}
    channel.timeline.extend([good, unreadable])
    recent = await channel.recent_posts(SINCE)
    assert recent.complete is False
    assert [p.post_id for p in recent.posts] == ["900"]


def _mention(i: int, **extra) -> dict:
    return {
        "id": str(800 + i),
        "text": f"@constworks question {i} &amp; more",
        "author_id": "77",
        "created_at": f"2026-09-26T02:0{i}:00.000Z",
        "conversation_id": "700",
        "public_metrics": {"like_count": i, "reply_count": 0, "retweet_count": 1,
                           "quote_count": 0},
        **extra,
    }  # fmt: skip


async def test_mentions_parse_authors_replies_and_metrics(channel, fake_x):
    channel.mentioned.extend(
        [_mention(0, referenced_tweets=[{"type": "replied_to", "id": "700"}]), _mention(1)]
    )
    page = await channel.mentions(SINCE, max_posts=50)
    assert page.complete and page.fetched == 2
    first = page.posts[0]
    assert first.author == "alice" and first.url == "https://x.com/alice/status/800"
    assert first.text == "@constworks question 0 & more", "X's entities are unescaped"
    assert first.reply_to == "700" and first.conversation_id == "700"
    assert first.metrics.likes == 0 and first.metrics.reposts == 1
    assert first.metrics.impressions is None, "X gives impressions only to the author"
    assert page.posts[1].reply_to is None
    request = fake_x.calls("GET")[0]
    assert request.url.params["start_time"] == "2026-09-26T01:00:00Z"
    assert request.url.params["expansions"] == "author_id"


async def test_mentions_ask_for_no_more_than_is_wanted(channel, fake_x):
    channel.mentioned.extend(_mention(i % 10) for i in range(12))
    page = await channel.mentions(SINCE, max_posts=7)
    sizes = [int(r.url.params["max_results"]) for r in fake_x.calls("GET")]
    assert sizes == [7], "one page of exactly what is wanted"
    assert len(page.posts) == 7 and page.fetched == 7 and not page.complete


async def test_a_page_minimum_overshoot_is_billed_but_not_returned(channel, fake_x):
    channel.mentioned.extend(_mention(i) for i in range(6))
    page = await channel.mentions(SINCE, max_posts=2)
    assert [int(r.url.params["max_results"]) for r in fake_x.calls("GET")] == [5]
    assert len(page.posts) == 2 and page.fetched == 5 and not page.complete


async def test_mentions_stop_at_the_page_cap(channel, fake_x, monkeypatch):
    monkeypatch.setattr("pulsar.app.core.channels.x.adapter.READ_PAGE_MAX", 5)
    monkeypatch.setattr("pulsar.app.core.channels.x.adapter.READ_MAX_PAGES", 2)
    channel.mentioned.extend(_mention(i % 10) for i in range(12))
    page = await channel.mentions(SINCE, max_posts=50)
    assert len(fake_x.calls("GET")) == 2
    assert len(page.posts) == 10 and not page.complete


async def test_an_unreadable_mention_makes_the_page_incomplete(channel):
    channel.mentioned.extend([_mention(0), {"id": "801", "text": "no time"}])
    page = await channel.mentions(SINCE, max_posts=50)
    assert [m.post_id for m in page.posts] == ["800"]
    assert not page.complete and page.fetched == 2


async def test_own_posts_carry_non_public_metrics(channel, fake_x):
    channel.timeline.append(
        {
            "id": "900",
            "text": "shipped",
            "created_at": "2026-09-26T01:30:00.000Z",
            "public_metrics": {
                "like_count": 4,
                "reply_count": 2,
                "retweet_count": 1,
                "quote_count": 0,
                "bookmark_count": 3,
                "impression_count": 90,
            },
            "non_public_metrics": {
                "impression_count": 120,
                "url_link_clicks": 5,
                "user_profile_clicks": 2,
            },
        }  # fmt: skip
    )
    page = await channel.own_posts(SINCE, max_posts=20)
    (post,) = page.posts
    assert post.url == "https://x.com/constworks/status/900"
    assert post.metrics.impressions == 120, "the author's own count wins"
    assert (post.metrics.likes, post.metrics.replies, post.metrics.bookmarks) == (4, 2, 3)
    assert (post.metrics.url_clicks, post.metrics.profile_clicks) == (5, 2)
    request = fake_x.calls("GET")[0]
    assert "non_public_metrics" in request.url.params["tweet.fields"]
    assert request.url.params["exclude"] == "retweets"
