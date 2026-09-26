import asyncio
import fcntl
import json
import multiprocessing
import os
import time
from pathlib import Path

import httpx
import pytest

from pulsar.core.errors import AuthExpired, OutcomeUnknown, PulsarError
from pulsar.core.paths import Paths
from pulsar.core.store import TokenBundle, TokenStore
from pulsar.providers.x.client import MediaProcessingError, XClient

from .conftest import (
    ACCESS,
    ALIAS,
    REFRESH,
    ROTATED_ACCESS,
    ROTATED_REFRESH,
    RotatingTokenEndpoint,
)

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
        # X may have posted before failing: never a retryable api_error.
        (500, {"title": "Internal"}, "outcome_unknown"),
        (503, {"title": "Service Unavailable"}, "outcome_unknown"),
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


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.elapsed

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.elapsed += seconds


async def test_upload_video_chunks_and_waits_for_success(store, authed, fake_x):
    fake_x.media_finalize_info = {"state": "pending", "check_after_secs": 2}
    fake_x.media_status_info = [
        {"state": "in_progress", "check_after_secs": 3},
        {"state": "succeeded"},
    ]
    clock = FakeClock()
    client = XClient(
        store, transport=fake_x.transport(), monotonic=clock.monotonic, sleep=clock.sleep
    )
    try:
        data = b"0" * (8 * 1024 * 1024 + 1)
        assert await client.upload_media(data, "video/mp4") == ("710000", "succeeded")
    finally:
        await client.aclose()
    assert clock.sleeps == [2, 3]
    assert json.loads(fake_x.calls("POST", "/media/upload/initialize")[0].content) == {
        "media_type": "video/mp4",
        "total_bytes": len(data),
        "media_category": "tweet_video",
    }
    assert len(fake_x.calls("POST", "/append")) == 3
    for index, request in enumerate(fake_x.calls("POST", "/append")):
        assert f'name="segment_index"\r\n\r\n{index}'.encode() in request.content
        assert len(request.content) < 5 * 1024 * 1024
    status_calls = fake_x.calls("GET", "/media/upload")
    assert len(status_calls) == 2
    assert all(
        dict(request.url.params) == {"command": "STATUS", "media_id": "710000"}
        for request in status_calls
    )


async def test_upload_video_processing_failure_preserves_x_detail(client, authed, fake_x):
    fake_x.media_finalize_info = {
        "state": "failed",
        "error": {"code": 3, "message": "Unsupported codec"},
    }
    with pytest.raises(MediaProcessingError) as exc:
        await client.upload_media(b"video", "video/mp4")
    assert exc.value.code == "invalid_media"
    assert exc.value.processing_state == "failed"
    assert exc.value.detail["error"]["message"] == "Unsupported codec"
    assert "Unsupported codec" in exc.value.message
    assert fake_x.calls("GET", "/media/upload") == []


async def test_upload_video_status_failure_preserves_x_detail(store, authed, fake_x):
    fake_x.media_finalize_info = {"state": "pending", "check_after_secs": 2}
    fake_x.media_status_info = [
        {"state": "failed", "error": {"code": 3, "message": "Transcoding rejected"}}
    ]
    clock = FakeClock()
    client = XClient(
        store, transport=fake_x.transport(), monotonic=clock.monotonic, sleep=clock.sleep
    )
    try:
        with pytest.raises(MediaProcessingError) as exc:
            await client.upload_media(b"video", "video/mp4")
    finally:
        await client.aclose()
    assert exc.value.code == "invalid_media"
    assert exc.value.detail["error"]["message"] == "Transcoding rejected"
    assert clock.sleeps == [2]
    assert len(fake_x.calls("GET", "/media/upload")) == 1


async def test_upload_video_without_processing_info_is_ready(client, authed, fake_x):
    fake_x.media_finalize_info = None
    assert await client.upload_media(b"video", "video/mp4") == ("710000", "succeeded")
    assert fake_x.calls("GET", "/media/upload") == []


async def test_upload_video_processing_timeout(store, authed, fake_x, monkeypatch):
    monkeypatch.setattr("pulsar.providers.x.client.PROCESSING_TIMEOUT_SECONDS", 3)
    fake_x.media_finalize_info = {"state": "pending", "check_after_secs": 2}
    fake_x.media_status_info = [{"state": "in_progress", "check_after_secs": 2}]
    clock = FakeClock()
    client = XClient(
        store, transport=fake_x.transport(), monotonic=clock.monotonic, sleep=clock.sleep
    )
    try:
        with pytest.raises(MediaProcessingError) as exc:
            await client.upload_media(b"video", "video/mp4")
    finally:
        await client.aclose()
    assert exc.value.code == "invalid_media"
    assert exc.value.processing_state == "timed_out"
    assert exc.value.detail["state"] == "in_progress"
    assert clock.sleeps == [2, 1]
    assert len(fake_x.calls("GET", "/media/upload")) == 1


# -- refresh across clients and processes -------------------------------------


def _expired(bundle):
    bundle.expires_at = time.time() - 10
    return bundle


async def test_two_clients_on_one_home_refresh_once(paths, bundle, token_endpoint):
    TokenStore.for_account(paths, ALIAS).save(_expired(bundle))
    token_endpoint.delay = 0.2  # the first refresher holds the lock across this
    clients = [
        XClient(TokenStore.for_account(paths, ALIAS), transport=token_endpoint.transport())
        for _ in range(2)
    ]
    try:
        tokens = await asyncio.gather(*(c.access_token() for c in clients))
    finally:
        for c in clients:
            await c.aclose()
    assert token_endpoint.calls() == 1
    assert tokens == ["access-gen1-YYYY", "access-gen1-YYYY"]
    assert TokenStore.for_account(paths, ALIAS).load().refresh_token == "refresh-gen1-ZZZZ"


def _refresh_in_child(home, state_file, barrier, results):
    async def run():
        endpoint = RotatingTokenEndpoint(state_file, delay=0.3)
        client = XClient(
            TokenStore.for_account(Paths(Path(home)), ALIAS), transport=endpoint.transport()
        )
        try:
            return await client.access_token()
        finally:
            await client.aclose()

    barrier.wait()
    try:
        results.put(("ok", asyncio.run(run())))
    except PulsarError as exc:
        results.put(("error", exc.code))


async def test_two_processes_on_one_home_refresh_once(paths, bundle, token_endpoint):
    TokenStore.for_account(paths, ALIAS).save(_expired(bundle))
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(
            target=_refresh_in_child,
            args=(str(paths.home), str(token_endpoint.state_file), barrier, results),
        )
        for _ in range(2)
    ]
    for p in procs:
        p.start()
    outcomes = [await asyncio.to_thread(results.get, timeout=60) for _ in procs]
    for p in procs:
        await asyncio.to_thread(p.join, 30)
        assert p.exitcode == 0
    assert outcomes == [("ok", "access-gen1-YYYY")] * 2
    assert token_endpoint.calls() == 1


async def test_refresh_waits_boundedly_for_the_lock(paths, authed, token_endpoint, monkeypatch):
    monkeypatch.setattr("pulsar.providers.x.client.REFRESH_LOCK_WAIT_SECONDS", 0.2)
    client = XClient(TokenStore.for_account(paths, ALIAS), transport=token_endpoint.transport())
    fd = os.open(TokenStore.for_account(paths, ALIAS).lock_file, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another process mid-refresh, and stuck
    try:
        with pytest.raises(PulsarError) as exc:
            await client.refresh(authed)
    finally:
        os.close(fd)
        await client.aclose()
    assert exc.value.code == "api_error"
    assert exc.value.detail["retryable"] is True
    assert token_endpoint.calls() == 0


async def test_rejected_refresh_uses_newer_bundle_from_a_lockless_process(
    paths, bundle, token_endpoint
):
    store = TokenStore.for_account(paths, ALIAS)
    store.save(_expired(bundle))

    async def rogue_rotates_first(request):
        # An older pulsar that ignores the lock rotated with the same refresh
        # token and saved its result while our POST was in flight.
        issued = token_endpoint.rotate(REFRESH, count=False)
        store.save(TokenBundle.from_token_response(issued, client_id=bundle.client_id))
        return await token_endpoint.handle(request)

    client = XClient(store, transport=httpx.MockTransport(rogue_rotates_first))
    try:
        assert await client.access_token() == "access-gen1-YYYY"
    finally:
        await client.aclose()
    assert token_endpoint.calls() == 1  # ours, rejected with invalid_grant
    assert store.load().refresh_token == "refresh-gen1-ZZZZ"


async def test_rejected_refresh_retries_with_newer_but_expiring_bundle(
    paths, bundle, token_endpoint
):
    store = TokenStore.for_account(paths, ALIAS)
    store.save(_expired(bundle))

    async def rogue_rotates_first(request):
        if token_endpoint.calls() == 0:
            issued = {**token_endpoint.rotate(REFRESH, count=False), "expires_in": 0}
            store.save(TokenBundle.from_token_response(issued, client_id=bundle.client_id))
        return await token_endpoint.handle(request)

    client = XClient(store, transport=httpx.MockTransport(rogue_rotates_first))
    try:
        assert await client.access_token() == "access-gen2-YYYY"
    finally:
        await client.aclose()
    assert token_endpoint.calls() == 2


async def test_genuinely_revoked_refresh_token_is_auth_expired(paths, bundle, token_endpoint):
    store = TokenStore.for_account(paths, ALIAS)
    store.save(_expired(bundle))
    token_endpoint.rotate(REFRESH, count=False)  # spent elsewhere; nothing newer saved here
    client = XClient(store, transport=token_endpoint.transport())
    try:
        with pytest.raises(AuthExpired):
            await client.access_token()
    finally:
        await client.aclose()
    assert token_endpoint.calls() == 1
    assert store.load().refresh_token == REFRESH  # a failed refresh never clobbers the store


async def test_refresh_does_not_clobber_a_concurrent_login(paths, bundle, token_endpoint):
    store = TokenStore.for_account(paths, ALIAS)
    store.save(_expired(bundle))
    relogin = TokenBundle(
        access_token="access-relogin-QQQQ",
        refresh_token="refresh-relogin-RRRR",
        expires_at=time.time() + 7200,
        scope="",
        client_id=bundle.client_id,
    )

    async def human_relogs_in_mid_refresh(request):
        response = await token_endpoint.handle(request)
        store.save(relogin)  # `pulsar auth login` does not take the refresh lock
        return response

    client = XClient(store, transport=httpx.MockTransport(human_relogs_in_mid_refresh))
    try:
        assert await client.access_token() == relogin.access_token
    finally:
        await client.aclose()
    assert store.load() == relogin


async def test_insecure_storage_is_not_auth_expired(paths, authed, fake_x):
    os.chmod(TokenStore.for_account(paths, ALIAS).token_file, 0o644)
    client = XClient(TokenStore.for_account(paths, ALIAS), transport=fake_x.transport())
    try:
        with pytest.raises(PulsarError) as exc:
            await client.me()
    finally:
        await client.aclose()
    assert exc.value.code == "insecure_storage"
    assert fake_x.requests == []


def _raising_transport(exc_type):
    def handler(request):
        raise exc_type("simulated", request=request)

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "exc_type",
    [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol],
)
async def test_not_sent_errors_on_a_post_are_retryable_api_errors(store, authed, exc_type):
    client = XClient(store, transport=_raising_transport(exc_type))
    try:
        with pytest.raises(PulsarError) as exc:
            await client.create_post("hi")
    finally:
        await client.aclose()
    assert exc.value.code == "api_error" and exc.value.retryable is True
    assert "not sent" in exc.value.message


@pytest.mark.parametrize(
    "exc_type",
    [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError],
)
async def test_maybe_sent_errors_on_a_post_are_outcome_unknown(store, authed, exc_type):
    client = XClient(store, transport=_raising_transport(exc_type))
    try:
        with pytest.raises(OutcomeUnknown) as exc:
            await client.create_post("hi")
    finally:
        await client.aclose()
    assert exc.value.code == "outcome_unknown" and exc.value.retryable is False
    assert exc.value.detail["cause"].startswith(exc_type.__name__)


async def test_maybe_sent_errors_on_a_read_stay_retryable(store, authed):
    """Only non-idempotent writes are ambiguous; a lost GET is just a failure."""
    client = XClient(store, transport=_raising_transport(httpx.ReadTimeout))
    try:
        with pytest.raises(PulsarError) as exc:
            await client.me()
    finally:
        await client.aclose()
    assert exc.value.code == "api_error" and exc.value.retryable is True


async def test_2xx_without_post_id_is_outcome_unknown(store, authed):
    client = XClient(
        store, transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"x": 1}))
    )
    try:
        with pytest.raises(OutcomeUnknown):
            await client.create_post("hi")
    finally:
        await client.aclose()


async def test_401_on_post_refreshes_and_posts_once(client, authed, fake_x):
    fake_x.fail_auth_once = True
    assert (await client.create_post("hi"))["post_id"] == "101"
    assert len(fake_x.calls("POST", "/oauth2/token")) == 1
    assert fake_x.next_post_id == 101


def test_error_results_carry_retryable():
    assert PulsarError("rate_limited", "wait").to_result()["retryable"] is True
    assert PulsarError("duplicate", "no").to_result()["retryable"] is False
    assert AuthExpired().to_result()["retryable"] is False
    assert OutcomeUnknown("ReadTimeout").to_result()["retryable"] is False


async def test_401_after_another_process_rotated_retries_without_refreshing(store, authed, fake_x):
    rotated = TokenBundle(
        access_token=ROTATED_ACCESS,
        refresh_token=ROTATED_REFRESH,
        expires_at=time.time() + 7200,
        scope=authed.scope,
        client_id=authed.client_id,
    )

    def handler(request):
        if request.headers.get("Authorization") == f"Bearer {ACCESS}":
            store.save(rotated)  # a sibling process refreshed while our call was in flight
            return httpx.Response(401, json={"title": "Unauthorized"})
        return fake_x.handle(request)

    client = XClient(store, transport=httpx.MockTransport(handler))
    try:
        assert (await client.me())["username"] == "constworks"
    finally:
        await client.aclose()
    assert fake_x.calls("POST", "/oauth2/token") == [], "no second rotation"


async def test_refresh_carries_the_binding_forward(paths, bundle, fake_x):
    store = TokenStore.for_account(paths, ALIAS)
    bound = store.rebind(_expired(bundle))
    client = XClient(store, transport=fake_x.transport())
    try:
        await client.access_token()
    finally:
        await client.aclose()
    stored = store.load()
    assert stored.access_token == ROTATED_ACCESS and stored.binding_id == bound.binding_id


async def test_client_delete_refuses_a_path_as_an_id(client, authed, fake_x):
    with pytest.raises(PulsarError) as exc:
        await client.delete_post("../users/1/retweets/555")
    assert exc.value.code == "invalid_argument" and fake_x.requests == []
