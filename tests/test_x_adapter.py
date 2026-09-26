import json
from datetime import UTC, datetime

import httpx
import pytest

from pulsar.channels import LoadedMedia
from pulsar.channels.x import XChannel, XClient, fingerprint, remote_fingerprint
from pulsar.errors import PulsarError
from pulsar.home import Prices
from pulsar.plan import MediaRef, PostSpec

from .media_samples import JPEG, MP4
from .media_samples import PNG as PNG_1PX

pytestmark = pytest.mark.anyio

SINCE = datetime(2026, 9, 26, 1, 0, tzinfo=UTC)


def _media(mime: str, data: bytes = PNG_1PX, alt: str = "a chart") -> LoadedMedia:
    return LoadedMedia(data=data, mime=mime, alt=alt, sha256="0" * 64)


@pytest.fixture
async def channel(store, authed, fake_x):
    timeline: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media/metadata"):
            fake_x.requests.append(request)
            return httpx.Response(200, json={"data": {"associated_metadata": True}})
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

    monkeypatch.setattr("pulsar.channels.x.adapter.RECONCILE_MAX_PAGES", 2)
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
