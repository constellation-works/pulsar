import json
import time

import pytest

from pulsar.errors import AuthExpired, PulsarError
from pulsar.xapi import XClient

from .conftest import ACCESS, ROTATED_ACCESS, ROTATED_REFRESH

pytestmark = pytest.mark.anyio


@pytest.fixture
async def client(store, fake_x):
    c = XClient(store, transport=fake_x.transport())
    yield c
    await c.aclose()


async def test_no_bundle_is_auth_expired(client):
    with pytest.raises(AuthExpired):
        await client.me()


async def test_me_uses_stored_token(client, authed, fake_x):
    assert await client.me() == {"user_id": "1234567890", "username": "constworks"}
    assert fake_x.calls("GET", "/users/me")[0].headers["Authorization"] == f"Bearer {ACCESS}"


async def test_refresh_ahead_of_expiry_rotates_and_persists(store, fake_x, bundle):
    bundle.expires_at = time.time() + 30  # inside the refresh-ahead window
    store.save(bundle)
    client = XClient(store, transport=fake_x.transport())
    try:
        await client.me()
    finally:
        await client.aclose()
    refresh = fake_x.calls("POST", "/oauth2/token")
    assert len(refresh) == 1
    assert b"grant_type=refresh_token" in refresh[0].content
    saved = store.load()
    assert saved.access_token == ROTATED_ACCESS
    assert saved.refresh_token == ROTATED_REFRESH
    assert (
        fake_x.calls("GET", "/users/me")[0].headers["Authorization"] == f"Bearer {ROTATED_ACCESS}"
    )


async def test_401_triggers_one_refresh_then_retry(client, authed, fake_x):
    fake_x.fail_auth_once = True
    assert (await client.me())["username"] == "constworks"
    assert len(fake_x.calls("POST", "/oauth2/token")) == 1
    assert len(fake_x.calls("GET", "/users/me")) == 2


async def test_refresh_failure_is_auth_expired_not_a_stack_trace(client, authed, fake_x):
    fake_x.fail_auth_once = True
    fake_x.refresh_status = 400
    with pytest.raises(AuthExpired) as exc:
        await client.me()
    assert exc.value.code == "auth_expired"


async def test_create_post_body_shape(client, authed, fake_x):
    out = await client.create_post("hi", reply_to_post_id="9", media_ids=["m1", "m2"])
    assert out == {"post_id": "101", "text": "hi"}
    body = json.loads(fake_x.calls("POST", "/tweets")[0].content)
    assert body == {
        "text": "hi",
        "reply": {"in_reply_to_tweet_id": "9"},
        "media": {"media_ids": ["m1", "m2"]},
    }


async def test_quote_post_body_shape(client, authed, fake_x):
    await client.create_post("hi", quote_post_id="77")
    body = json.loads(fake_x.calls("POST", "/tweets")[0].content)
    assert body == {"text": "hi", "quote_tweet_id": "77"}


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (
            403,
            {"detail": "You are not allowed to create a Tweet with duplicate content."},
            "duplicate",
        ),
        (403, {"detail": "Your account is suspended."}, "forbidden"),
        (429, {"title": "Too Many Requests"}, "rate_limited"),
        (500, {"title": "Internal"}, "api_error"),
    ],
)
async def test_http_errors_map_to_codes(client, authed, fake_x, status, body, code):
    fake_x.tweet_status, fake_x.tweet_body = status, body
    with pytest.raises(PulsarError) as exc:
        await client.create_post("hi")
    assert exc.value.code == code
    assert exc.value.detail


async def test_delete_post(client, authed, fake_x):
    assert await client.delete_post("101") is True
    assert fake_x.calls("DELETE")[0].url.path.endswith("/tweets/101")


async def test_upload_image_is_chunked_init_append_finalize(client, authed, fake_x):
    data = b"\x89PNG" + b"0" * (1024 * 1024 + 10)
    assert await client.upload_image(data, "image/png") == "710000"
    paths = [r.url.path.rsplit("/2", 1)[-1] for r in fake_x.requests]
    assert paths == [
        "/media/upload/initialize",
        "/media/upload/710000/append",
        "/media/upload/710000/append",
        "/media/upload/710000/finalize",
    ]
    init = json.loads(fake_x.requests[0].content)
    assert init == {
        "media_type": "image/png",
        "total_bytes": len(data),
        "media_category": "tweet_image",
    }
