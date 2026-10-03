"""Bluesky's own rules on the fake PDS: graphemes, facets, the records ``create``
builds, its client's auth (refresh, DPoP, pinning) and failure mapping. The
contract every channel keeps is in test_channel_contract.py."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from pulsar.app.core.channels.bluesky import (
    BlueskyChannel,
    BlueskyClient,
    Es256Proof,
    detect_facets,
    grapheme_count,
    validate_text,
)
from pulsar.app.core.channels.contract import FREE, LoadedMedia, PostSpec, Prices
from pulsar.internal.errors import PulsarError

from .conftest import ACCESS, ROTATED_ACCESS, register
from .fake_bsky import ALICE, ALICE_DID, DID, HANDLE, FakeBsky, jwt_claims
from .media_samples import JPEG, MP4, PNG

pytestmark = pytest.mark.anyio

ALIAS = "bsky:constworks.bsky.social"
SINCE = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture
def fake() -> FakeBsky:
    return FakeBsky()


@pytest.fixture
def bsky_store(paths, bundle):
    return register(paths, bundle, alias=ALIAS)


@pytest.fixture
async def channel(bsky_store, fake):
    ch = BlueskyChannel(
        BlueskyClient(bsky_store, transport=fake.transport()),
        did=DID,
        handle=HANDLE,
        now=lambda: NOW,
    )
    yield ch
    await ch.aclose()


def _media(mime: str, data: bytes = PNG, alt: str = "a chart") -> LoadedMedia:
    return LoadedMedia(data=data, mime=mime, alt=alt, sha256=hashlib.sha256(data).hexdigest())


# -- graphemes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "count"),
    [
        ("hello", 5),
        ("e\u0301te\u0301", 3),  # decomposed accents stay with their letter
        ("👨‍👩‍👧‍👦", 1),  # a ZWJ family
        ("👍🏽👍", 2),  # a skin-tone modifier extends
        ("🇺🇸🇫🇷", 2),  # regional indicators pair up
        ("🇺🇸🇫", 2),  # an odd one out stands alone
        ("1\ufe0f\u20e3", 1),  # a keycap
        ("한국어", 3),
        ("\u1112\u1161\u11ab", 1),  # conjoining jamo: one syllable
        ("a\r\nb", 3),
        (
            "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
            1,
        ),  # a subdivision flag (tag sequence)
    ],
)
def test_grapheme_count(text, count):
    assert grapheme_count(text) == count


def test_the_limit_is_300_graphemes_and_3000_bytes():
    family = "👨‍👩‍👧‍👦"
    assert validate_text("é" * 300, FREE).graphemes == 300  # 600 bytes, 300 graphemes
    with pytest.raises(PulsarError) as exc:
        validate_text("a" * 301, FREE)
    assert exc.value.code == "invalid_text" and exc.value.detail["graphemes"] == 301
    with pytest.raises(PulsarError) as exc:
        validate_text(family * 121, FREE)  # 121 graphemes, 3025 bytes
    assert exc.value.code == "invalid_text" and exc.value.detail["limit"] == 3000
    with pytest.raises(PulsarError) as exc:
        validate_text("bell\x07", FREE)
    assert exc.value.code == "invalid_text"


# -- facets -------------------------------------------------------------------------


def _spans(text: str) -> list[tuple[str, str, str]]:
    raw = text.encode("utf-8")
    return [
        (f.kind, raw[f.byte_start : f.byte_end].decode("utf-8"), f.value)
        for f in detect_facets(text)
    ]


def test_facets_are_utf8_byte_ranges():
    text = "Café ☕ notes: https://example.com/notes. Thanks @Alice.bsky.social! #Launch2026"
    assert _spans(text) == [
        ("link", "https://example.com/notes", "https://example.com/notes"),
        ("mention", "@Alice.bsky.social", "alice.bsky.social"),
        ("tag", "#Launch2026", "Launch2026"),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see example.com/docs", [("link", "example.com/docs", "https://example.com/docs")]),
        ("(https://x.org/a)", [("link", "https://x.org/a", "https://x.org/a")]),
        ("Orbit v0.25 ships", []),  # no alphabetic TLD
        ("mail me@example.com", []),  # not after whitespace
        ("hi @alice", []),  # a handle has a domain
        ("#123 and #️⃣ and # alone", []),
        ("#tag, #two.", [("tag", "#tag", "tag"), ("tag", "#two", "two")]),
        ("\uff03fullwidth", [("tag", "\uff03fullwidth", "fullwidth")]),
    ],
)
def test_facet_detection(text, expected):
    assert _spans(text) == expected


def test_a_url_is_reported_and_priced():
    assert validate_text("read https://example.com", Prices(0.0, 0.5)).has_url
    assert validate_text("read https://example.com", Prices(0.0, 0.5)).estimated_cost_usd == 0.5
    assert not validate_text("nothing to see", Prices()).has_url


# -- create ---------------------------------------------------------------------------


async def test_create_writes_a_post_record_with_resolved_facets(channel, fake):
    text = "Hi @alice.bsky.social and @nobody.example — https://example.com #ship"
    published = await channel.create(text)
    (call,) = fake.bodies("com.atproto.repo.createRecord")
    record = call["record"]
    assert call["repo"] == DID and call["collection"] == "app.bsky.feed.post"
    assert record["text"] == text and record["createdAt"] == "2026-09-26T12:00:00.000Z"
    features = [f["features"][0] for f in record["facets"]]
    assert features == [
        {"$type": "app.bsky.richtext.facet#mention", "did": ALICE_DID},
        {"$type": "app.bsky.richtext.facet#link", "uri": "https://example.com"},
        {"$type": "app.bsky.richtext.facet#tag", "tag": "ship"},
    ], "an unresolvable handle stays plain text"
    rkey = published.post_id.rsplit("/", 1)[-1]
    assert published.url == f"https://bsky.app/profile/{HANDLE}/post/{rkey}"
    assert published.post_id == f"at://{DID}/app.bsky.feed.post/{rkey}"


async def test_a_reply_to_a_reply_keeps_the_threads_root(channel, fake):
    root = fake.add_other("3jroot", "the root")
    root_ref = {"uri": root, "cid": fake.others[root]["cid"]}
    middle = fake.add_other("3jmiddle", "a reply", reply={"root": root_ref, "parent": root_ref})
    await channel.create("our answer", reply_to=middle)
    (record,) = fake.posts()
    assert record["reply"] == {
        "root": root_ref,
        "parent": {"uri": middle, "cid": fake.others[middle]["cid"]},
    }


async def test_a_thread_replies_from_memory_and_a_resumed_one_from_the_repo(
    channel, fake, bsky_store
):
    first = await channel.create("one")
    await channel.create("two", reply_to=first.post_id)
    assert fake.calls("com.atproto.repo.getRecord") == [], "the channel remembers its posts"
    one, two = fake.posts()
    first_ref = {"uri": first.post_id, "cid": fake.records[first.post_id.rsplit("/", 1)[-1]]["cid"]}
    assert two["reply"] == {"root": first_ref, "parent": first_ref}

    resumed = BlueskyChannel(
        BlueskyClient(bsky_store, transport=fake.transport()), did=DID, handle=HANDLE
    )
    try:
        await resumed.create("three", reply_to=first.post_id)
    finally:
        await resumed.aclose()
    assert len(fake.calls("com.atproto.repo.getRecord")) == 1
    assert fake.posts()[-1]["reply"]["root"] == first_ref


async def test_a_reply_to_a_missing_post_sends_nothing(channel, fake):
    with pytest.raises(PulsarError) as exc:
        await channel.create("into the void", reply_to=f"at://{ALICE_DID}/app.bsky.feed.post/3jx")
    assert exc.value.code == "not_found"
    assert fake.calls("com.atproto.repo.createRecord") == []


async def test_images_embed_with_their_alt_text_and_video_alone(channel, fake):
    ids = [
        await channel.upload(_media("image/png")),
        await channel.upload(_media("image/jpeg", JPEG, "a photo")),
    ]
    await channel.create("two images", media_ids=tuple(ids))
    video = await channel.upload(_media("video/mp4", MP4, "a demo"))
    await channel.create("a video", media_ids=(video,))
    images, clip = (p["embed"] for p in fake.posts())
    assert images["$type"] == "app.bsky.embed.images"
    assert [(i["alt"], i["image"]["mimeType"]) for i in images["images"]] == [
        ("a chart", "image/png"),
        ("a photo", "image/jpeg"),
    ]
    assert clip["$type"] == "app.bsky.embed.video" and clip["alt"] == "a demo"
    assert [r.headers["Content-Type"] for r in fake.calls("com.atproto.repo.uploadBlob")] == [
        "image/png",
        "image/jpeg",
        "video/mp4",
    ]


async def test_a_quote_with_media_is_a_record_with_media_embed(channel, fake):
    quoted = fake.add_other("3jquoted", "quote me")
    image = await channel.upload(_media("image/png"))
    await channel.create("look", quote=quoted, media_ids=(image,))
    (record,) = fake.posts()
    embed = record["embed"]
    assert embed["$type"] == "app.bsky.embed.recordWithMedia"
    assert embed["record"]["record"] == {"uri": quoted, "cid": fake.others[quoted]["cid"]}
    assert embed["media"]["images"][0]["alt"] == "a chart"


async def test_a_media_id_from_elsewhere_is_refused_before_sending(channel, fake):
    with pytest.raises(PulsarError) as exc:
        await channel.create("hi", media_ids=("bafkreiunknown",))
    assert exc.value.code == "invalid_media"
    assert fake.calls("com.atproto.repo.createRecord") == []


@pytest.mark.parametrize(
    ("mimes", "alt", "ok"),
    [
        (["image/png"] * 4, "a", True),
        (["image/png"] * 5, "a", False),
        (["video/mp4"], "a", True),
        (["video/mp4", "image/png"], "a", False),
        (["image/png"], "x" * 2000, True),
        (["image/png"], "x" * 2001, False),
        (["video/mp4"], "x" * 1001, False),
    ],
)
async def test_check_media(channel, mimes, alt, ok):
    media = tuple(_media(m, alt=alt) for m in mimes)
    if ok:
        channel.check_media(media)
    else:
        with pytest.raises(PulsarError) as exc:
            channel.check_media(media)
        assert exc.value.code == "invalid_media"


async def test_capabilities_narrow_image_size_to_bluesky(channel):
    assert channel.capabilities.media.limit_for("image/png") == 1_000_000
    assert channel.capabilities.media.limit_for("video/mp4") == 100_000_000
    assert channel.capabilities.length_unit == "graphemes"
    report = channel.check_post(PostSpec(text="ok"), FREE)
    assert report.estimated_cost_usd == 0.0


# -- failures -----------------------------------------------------------------------------


async def test_a_5xx_on_create_is_outcome_unknown(channel, fake):
    fake.create_status = 502
    with pytest.raises(PulsarError) as exc:
        await channel.create("maybe")
    assert exc.value.code == "outcome_unknown" and not exc.value.retryable


async def test_a_refused_record_is_a_definite_failure(channel, fake):
    fake.create_status = 400
    with pytest.raises(PulsarError) as exc:
        await channel.create("no")
    assert exc.value.code == "api_error"


async def test_a_connection_that_never_opened_is_retryable(bsky_store):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    ch = BlueskyChannel(
        BlueskyClient(bsky_store, transport=httpx.MockTransport(refuse)), did=DID, handle=HANDLE
    )
    try:
        with pytest.raises(PulsarError) as exc:
            await ch.create("hi")
    finally:
        await ch.aclose()
    assert exc.value.code == "api_error" and exc.value.retryable


async def test_delete_refuses_another_accounts_post(channel, fake):
    with pytest.raises(PulsarError) as exc:
        await channel.delete(f"at://{ALICE_DID}/app.bsky.feed.post/3jx")
    assert exc.value.code == "invalid_argument"
    assert fake.calls("com.atproto.repo.deleteRecord") == []


# -- auth -------------------------------------------------------------------------------


async def test_an_expired_token_is_refreshed_once_and_saved(channel, fake, bsky_store):
    fake.fail_auth_once = True
    assert (await channel.whoami()).provider_user_id == DID
    assert len(fake.calls("com.atproto.server.getSession")) == 2
    assert bsky_store.load().access_token == ROTATED_ACCESS


async def test_a_refused_refresh_is_auth_expired(channel, fake):
    fake.fail_auth_once, fake.refresh_status = True, 400
    with pytest.raises(PulsarError) as exc:
        await channel.whoami()
    assert exc.value.code == "auth_expired"


async def test_a_pinned_client_refuses_a_rebound_login(bsky_store, bundle, fake):
    client = BlueskyClient(bsky_store, transport=fake.transport())
    try:
        with pytest.raises(PulsarError) as exc:
            await client.pinned("another-binding").me()
    finally:
        await client.aclose()
    assert exc.value.code == "account_mismatch"
    assert fake.requests == []


async def test_a_dpop_bound_token_without_a_key_sends_nothing(paths, bundle, fake):
    store = register(paths, replace(bundle, token_type="DPoP"), alias=ALIAS)
    client = BlueskyClient(store, transport=fake.transport())
    try:
        with pytest.raises(PulsarError) as exc:
            await client.me()
    finally:
        await client.aclose()
    assert exc.value.code == "unsupported"
    assert fake.requests == []


def _verify(proof: str, key: ec.EllipticCurvePrivateKey) -> dict:
    head, body, sig = proof.split(".")
    header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    r, s = int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
    key.public_key().verify(
        encode_dss_signature(r, s), f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256())
    )
    return jwt_claims(proof)


async def test_dpop_proofs_learn_the_nonce_and_sign_each_request(paths, bundle, fake):
    key = ec.generate_private_key(ec.SECP256R1())
    fake.dpop_nonce = "nonce-1"
    store = register(paths, replace(bundle, token_type="DPoP"), alias=ALIAS)
    client = BlueskyClient(
        store, transport=fake.transport(), service="https://pds.example", proof=Es256Proof(key)
    )
    try:
        me = await client.me()
        await client.query("com.atproto.repo.listRecords", {"repo": DID, "limit": 1})
    finally:
        await client.aclose()
    assert me == {"user_id": DID, "username": HANDLE}
    first, retried, listed = fake.requests
    assert "nonce" not in jwt_claims(first.headers["DPoP"]), "no nonce known yet"
    claims = _verify(retried.headers["DPoP"], key)
    assert retried.headers["Authorization"] == f"DPoP {ACCESS}"
    assert claims["htm"] == "GET" and claims["nonce"] == "nonce-1"
    assert claims["htu"] == "https://pds.example/xrpc/com.atproto.server.getSession"
    expected_ath = hashlib.sha256(ACCESS.encode()).digest()
    assert claims["ath"] == base64.urlsafe_b64encode(expected_ath).rstrip(b"=").decode()
    listed_claims = _verify(listed.headers["DPoP"], key)
    assert listed_claims["htu"] == "https://pds.example/xrpc/com.atproto.repo.listRecords"
    assert listed_claims["nonce"] == "nonce-1", "the nonce is remembered"
    assert listed_claims["jti"] != claims["jti"]


async def test_a_dpop_refresh_signs_the_token_request(paths, bundle, fake):
    key = ec.generate_private_key(ec.SECP256R1())
    fake.dpop_nonce = "nonce-2"
    expiring = replace(bundle, token_type="DPoP", expires_at=time.time() - 1)
    store = register(paths, expiring, alias=ALIAS)
    client = BlueskyClient(
        store,
        transport=fake.transport(),
        token_url="https://bsky.social/oauth/token",
        proof=Es256Proof(key),
    )
    try:
        await client.me()
    finally:
        await client.aclose()
    token_calls = [r for r in fake.requests if r.url.path == "/oauth/token"]
    assert len(token_calls) == 2, "the first token request learns the nonce"
    claims = _verify(token_calls[-1].headers["DPoP"], key)
    assert claims["htu"] == "https://bsky.social/oauth/token" and "ath" not in claims
    saved = store.load()
    assert saved.access_token == ROTATED_ACCESS and saved.token_type == "DPoP"
    assert saved.binding_id == expiring.binding_id


def test_a_dpop_key_must_be_p256():
    with pytest.raises(ValueError):
        Es256Proof(ec.generate_private_key(ec.SECP384R1()))


# -- reconcile and reads ---------------------------------------------------------------


async def test_recent_posts_stop_at_since_and_mark_unreadable_records(channel, fake):
    old = await channel.create("old")
    channel._now = lambda: NOW + timedelta(hours=1)  # type: ignore[method-assign]
    new = await channel.create("new")
    recent = await channel.recent_posts(NOW + timedelta(minutes=30))
    assert [p.post_id for p in recent.posts] == [new.post_id] and recent.complete
    assert old.post_id not in {p.post_id for p in recent.posts}
    fake.records["3mbroken"] = {"uri": "at://nope", "cid": "x", "value": {"text": "?"}}
    recent = await channel.recent_posts(SINCE)
    assert not recent.complete, "an unreadable record is no evidence of absence"


async def test_recent_posts_hit_the_page_cap_as_incomplete(channel, fake):
    for i in range(7):
        await channel.create(f"post {i}")
    fake.page_size = 1
    recent = await channel.recent_posts(SINCE)
    assert len(recent.posts) == 5 and not recent.complete


async def test_mentions_carry_thread_refs_and_counts(channel, fake):
    mine = await channel.create("our launch")
    mine_ref = {"uri": mine.post_id, "cid": fake.records[mine.post_id.rsplit("/", 1)[-1]]["cid"]}
    reply = fake.mention("3kreply", "congrats!", reason="reply",
                         created_at="2026-09-26T13:00:00.000Z",
                         reply={"root": mine_ref, "parent": mine_ref})  # fmt: skip
    fake.notifications.insert(0, {**fake.notifications[0], "reason": "like", "uri": "at://x"})
    fake.counts[reply] = {"likeCount": 3, "repostCount": 1}
    page = await channel.mentions(SINCE, max_posts=5)
    (m,) = page.posts
    assert m.post_id == reply and m.author == ALICE and m.text == "congrats!"
    assert m.reply_to == mine.post_id and m.conversation_id == mine.post_id
    assert m.metrics.likes == 3 and m.metrics.reposts == 1 and m.metrics.impressions is None
    assert m.url == f"https://bsky.app/profile/{ALICE}/post/3kreply"
    (call,) = fake.calls("app.bsky.notification.listNotifications")
    assert call.url.params.get_list("reasons") == ["mention", "reply", "quote"]


async def test_mentions_before_since_end_the_listing(channel, fake):
    fake.mention("3kold", "old news", created_at="2026-09-25T10:00:00.000Z")
    fake.mention("3knew", "fresh", created_at="2026-09-26T10:00:00.000Z")
    page = await channel.mentions(SINCE, max_posts=5)
    assert [m.text for m in page.posts] == ["fresh"] and page.complete


async def test_own_posts_leave_out_reposts_and_cap_at_max_posts(channel, fake):
    for i in range(3):
        await channel.create(f"mine {i}")
    alice = fake.add_other("3jalice", "alice's")
    fake.reposts.append({"post": fake._view(alice), "reason": {"$type": "reasonRepost"}})
    page = await channel.own_posts(SINCE, max_posts=2)
    assert [p.text for p in page.posts] == ["mine 2", "mine 1"] and not page.complete
    page = await channel.own_posts(SINCE, max_posts=10)
    assert [p.text for p in page.posts] == ["mine 2", "mine 1", "mine 0"] and page.complete
    (call,) = fake.calls("app.bsky.feed.getAuthorFeed")[-1:]
    assert call.url.params["actor"] == DID and call.url.params["filter"] == "posts_with_replies"
