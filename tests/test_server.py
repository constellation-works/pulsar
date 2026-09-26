"""End-to-end through a real MCP client session over the in-memory transport."""

import base64
import json
import re

import httpx
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from pulsar.core.accounts import AccountRegistry
from pulsar.core.adapter import Identity
from pulsar.core.media import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES
from pulsar.core.settings import Settings
from pulsar.core.store import TokenBundle
from pulsar.surfaces.mcp import TOOL_NAMES, Runtime, build_server

from .conftest import ALIAS, SECRETS, register
from .media_samples import JPEG, MP4, PEM_KEY
from .media_samples import PNG as PNG_1PX

SECRET_PARAM = re.compile(r"(?i)token|secret|bearer|password|api[_-]?key|client[_-]?secret")

pytestmark = pytest.mark.anyio


@pytest.fixture
async def session(paths, fake_x, tmp_path):
    rt = Runtime(
        paths, settings=Settings(media_roots=(tmp_path / "media",)), transport=fake_x.transport()
    )
    server = build_server(rt)
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s
    await rt.aclose()


@pytest.fixture
def media_dir(tmp_path, monkeypatch):
    """The session's configured media root, also the cwd for relative paths."""
    d = tmp_path / "media"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def _payload(result):
    assert not result.is_error, result
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _no_secret_leak(obj):
    blob = json.dumps(obj)
    for s in SECRETS:
        assert s not in blob


async def test_tool_list_is_exactly_the_five_and_has_no_secret_params(session):
    tools = (await session.list_tools()).tools
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    for t in tools:
        for prop in t.input_schema.get("properties", {}):
            assert not SECRET_PARAM.search(prop), (
                f"{t.name}.{prop} looks like a credential parameter"
            )


async def test_annotations_let_a_harness_gate_by_name(session):
    """The policy boundary is the caller's; these hints are how it finds it."""
    by_name = {t.name: t.annotations for t in (await session.list_tools()).tools}
    assert all(by_name[n] is not None for n in TOOL_NAMES)
    assert by_name["whoami"].read_only_hint is True
    assert by_name["validate_post"].read_only_hint is True
    assert by_name["validate_post"].destructive_hint is False
    for publishes in ("create_post", "upload_media"):
        assert by_name[publishes].read_only_hint is False
        assert by_name[publishes].destructive_hint is False
    assert by_name["delete_post"].read_only_hint is False
    assert by_name["delete_post"].destructive_hint is True


async def test_validate_post_is_local_and_unlogged(session, authed, fake_x, paths):
    text = "hello from pulsar"
    out = _payload(await session.call_tool("validate_post", {"text": text}))
    assert out["ok"] is True
    assert "dry_run" not in out
    assert out["weighted_length"] == len(text) and out["has_url"] is False
    assert fake_x.requests == []
    assert not paths.write_log.exists(), "validation is not a write; it must not hit the log"


async def test_validate_post_matches_legacy_dry_run(session, authed):
    text = "see https://x.com/constworks"
    new = _payload(await session.call_tool("validate_post", {"text": text}))
    old = _payload(await session.call_tool("create_post", {"text": text, "dry_run": True}))
    assert old.pop("dry_run") is True
    assert new == old


async def test_validate_post_rejects_reply_plus_quote(session, authed):
    out = _payload(
        await session.call_tool(
            "validate_post", {"text": "x", "reply_to_post_id": "1", "quote_post_id": "2"}
        )
    )
    assert out == {
        "ok": False,
        "code": "invalid_text",
        "message": "a post cannot be both a reply and a quote in v1",
        "retryable": False,
    }


@pytest.mark.parametrize(
    "secret", ["sk-abcdefghijklmnopqrstuvwxyz", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"]
)
async def test_validate_post_rejects_secrets(session, authed, fake_x, secret):
    out = _payload(await session.call_tool("validate_post", {"text": f"oops {secret}"}))
    assert out["ok"] is False and out["code"] == "secret_detected"
    assert fake_x.requests == []


async def test_whoami_is_cached_locally(session, authed, fake_x, paths):
    out = _payload(await session.call_tool("whoami", {}))
    assert out == {"ok": True, "user_id": "1234567890", "username": "constworks"}
    _payload(await session.call_tool("whoami", {}))
    assert len(fake_x.calls("GET", "/users/me")) == 1
    row = AccountRegistry(paths).get(ALIAS)
    assert (row.handle, row.provider_user_id) == ("constworks", "1234567890")
    _no_secret_leak(out)


async def test_whoami_without_auth_is_auth_expired(session):
    out = _payload(await session.call_tool("whoami", {}))
    assert out["ok"] is False and out["code"] == "auth_expired"


async def test_dry_run_validates_without_network(session, authed, fake_x, paths):
    text = "Hello from the constellation — pulsar is live. Posts now flow through an MCP bridge."
    out = _payload(
        await session.call_tool("create_post", {"text": text, "dry_run": True, "caller": "test"})
    )
    assert out["ok"] is True and out["dry_run"] is True
    assert out["text"] == text
    assert out["estimated_cost_usd"] == 0.015
    assert fake_x.requests == []
    assert not paths.write_log.exists(), "a dry run is not a write; it must not hit the log"
    assert not paths.ledger_db.exists(), "a dry run is not a write; it must not hit the ledger"


async def test_dry_run_with_url_costs_more(session, authed):
    out = _payload(
        await session.call_tool(
            "create_post", {"text": "see https://x.com/constworks", "dry_run": True}
        )
    )
    assert out["estimated_cost_usd"] == 0.20 and out["has_url"] is True


@pytest.mark.parametrize(
    "secret", ["sk-abcdefghijklmnopqrstuvwxyz", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"]
)
async def test_secret_in_text_is_rejected_even_on_dry_run(session, authed, fake_x, secret):
    out = _payload(
        await session.call_tool("create_post", {"text": f"oops {secret}", "dry_run": True})
    )
    assert out["ok"] is False and out["code"] == "secret_detected"
    out = _payload(await session.call_tool("create_post", {"text": f"oops {secret}"}))
    assert out["code"] == "secret_detected"
    assert fake_x.requests == []


async def test_live_create_post_returns_url_and_logs(session, authed, fake_x, paths, monkeypatch):
    monkeypatch.setenv("PULSAR_CALLER", "grokbot/constellation-supervisor")
    out = _payload(await session.call_tool("create_post", {"text": "first light"}))
    assert out == {
        "ok": True,
        "post_id": "101",
        "url": "https://x.com/constworks/status/101",
        "text": "first light",
    }
    _no_secret_leak(out)
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["tool"] == "create_post" and line["post_id"] == "101"
    assert line["caller"] == "grokbot/constellation-supervisor"
    assert line["text_sha256"] == __import__("hashlib").sha256(b"first light").hexdigest()
    assert "first light" not in json.dumps(line)


async def test_reply_and_quote_together_is_invalid(session, authed, fake_x):
    out = _payload(
        await session.call_tool(
            "create_post", {"text": "x", "reply_to_post_id": "1", "quote_post_id": "2"}
        )
    )
    assert out["code"] == "invalid_text" and fake_x.requests == []


async def test_x_duplicate_passes_through(session, authed, fake_x):
    fake_x.tweet_status = 403
    fake_x.tweet_body = {"detail": "duplicate content"}
    out = _payload(await session.call_tool("create_post", {"text": "again"}))
    assert out["ok"] is False and out["code"] == "duplicate"
    assert "duplicate" in json.dumps(out["detail"])


async def test_refresh_failure_surfaces_as_auth_expired(session, authed, fake_x):
    fake_x.fail_auth_once = True
    fake_x.refresh_status = 401
    out = _payload(await session.call_tool("create_post", {"text": "hello"}))
    assert out["ok"] is False and out["code"] == "auth_expired"
    assert out["retryable"] is False
    # The remedy names the account and the home it lives under.
    assert out["message"].startswith("X refused the refresh token (HTTP 401)")
    assert "PULSAR_HOME=" in out["message"]
    assert "pulsar auth login --account x:constworks" in out["message"]


async def test_upload_media_by_path_then_attach(session, authed, fake_x, media_dir):
    png = media_dir / "pic.png"
    png.write_bytes(PNG_1PX)
    up = _payload(await session.call_tool("upload_media", {"path": str(png)}))
    assert up["ok"] is True and up["media_id"] == "710000" and up["mime"] == "image/png"
    post = _payload(
        await session.call_tool("create_post", {"text": "with pic", "media_ids": [up["media_id"]]})
    )
    assert post["ok"] is True
    body = json.loads(fake_x.calls("POST", "/tweets")[0].content)
    assert body["media"] == {"media_ids": ["710000"]}


async def test_upload_media_by_base64(session, authed):
    up = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(PNG_1PX).decode(), "mime": "image/png"}
        )
    )
    assert up["ok"] is True and up["bytes"] == len(PNG_1PX)


async def test_upload_video_by_path_and_log(session, authed, fake_x, paths, media_dir):
    video = media_dir / "reel.mp4"
    video.write_bytes(MP4)
    up = _payload(await session.call_tool("upload_media", {"path": str(video)}))
    assert up == {
        "ok": True,
        "media_id": "710000",
        "mime": "video/mp4",
        "bytes": len(video.read_bytes()),
    }
    init = json.loads(fake_x.calls("POST", "/media/upload/initialize")[0].content)
    assert init["media_category"] == "tweet_video"
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["mime"] == "video/mp4"
    assert line["bytes"] == len(video.read_bytes())
    assert line["processing_state"] == "succeeded"
    assert "distinct-video-payload" not in json.dumps(line)
    _no_secret_leak(line)


async def test_upload_video_by_base64(session, authed):
    data = MP4
    up = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(data).decode(), "mime": "video/mp4"}
        )
    )
    assert up["ok"] is True and up["mime"] == "video/mp4" and up["bytes"] == len(data)


async def test_upload_video_failure_logs_final_state_without_media_id(
    session, authed, fake_x, paths
):
    fake_x.media_finalize_info = {
        "state": "failed",
        "error": {"code": 3, "message": "Unsupported codec"},
    }
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(MP4).decode(), "mime": "video/mp4"}
        )
    )
    assert out["ok"] is False and out["code"] == "invalid_media"
    assert "media_id" not in out
    assert out["detail"]["error"]["message"] == "Unsupported codec"
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["processing_state"] == "failed"
    assert line["mime"] == "video/mp4" and line["bytes"] == len(MP4)
    assert "media_id" not in line


async def test_upload_video_timeout_has_no_media_id_and_logs_state(
    session, authed, fake_x, paths, monkeypatch
):
    monkeypatch.setattr("pulsar.providers.x.client.PROCESSING_TIMEOUT_SECONDS", 0)
    fake_x.media_finalize_info = {"state": "pending", "check_after_secs": 2}
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(MP4).decode(), "mime": "video/mp4"}
        )
    )
    assert out["code"] == "invalid_media" and "media_id" not in out
    assert out["detail"]["state"] == "pending"
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["processing_state"] == "timed_out"


async def test_upload_media_scans_bytes_before_x_write(session, authed, fake_x):
    data = MP4 + b"prefix sk-abcdefghijklmnopqrstuvwxyz suffix"
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(data).decode(), "mime": "video/mp4"}
        )
    )
    assert out["code"] == "secret_detected"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(out)
    assert fake_x.requests == []


async def test_upload_media_limits_and_mime(session, authed, fake_x, media_dir):
    for name, size, needle in (
        ("clip.webm", 1, "unsupported media type video/webm"),
        ("large.mp4", MAX_VIDEO_BYTES + 1, str(MAX_VIDEO_BYTES)),
        ("large.png", MAX_IMAGE_BYTES + 1, str(MAX_IMAGE_BYTES)),
    ):
        media = media_dir / name
        with media.open("wb") as fh:
            fh.truncate(size)
        out = _payload(await session.call_tool("upload_media", {"path": str(media)}))
        assert out["ok"] is False and out["code"] == "invalid_media"
        assert needle in out["message"]
    assert fake_x.requests == []


async def test_upload_video_base64_limit(session, authed, fake_x, monkeypatch):
    monkeypatch.setattr("pulsar.core.media.MAX_VIDEO_BYTES", 4)
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(MP4).decode(), "mime": "video/mp4"}
        )
    )
    assert out["code"] == "invalid_media" and "limit is 4" in out["message"]
    assert fake_x.requests == []


async def test_upload_media_description_documents_video_limits(session):
    by_name = {tool.name: tool for tool in (await session.list_tools()).tools}
    description = by_name["upload_media"].description
    assert "video/mp4" in description and "100 MiB" in description
    assert "5 MiB" in description


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({}, "exactly one"),
        ({"path": "/nonexistent/file.png"}, "no such file"),
        ({"base64": "not base64!!", "mime": "image/png"}, "not valid"),
        ({"base64": base64.b64encode(b"abc").decode(), "mime": "video/webm"}, "unsupported"),
    ],
)
async def test_upload_media_rejections(session, authed, fake_x, args, needle):
    out = _payload(await session.call_tool("upload_media", args))
    assert out["ok"] is False and out["code"] == "invalid_media" and needle in out["message"]
    assert fake_x.requests == []


async def test_upload_media_refuses_a_private_key_outside_the_roots(
    session, authed, fake_x, media_dir, tmp_path
):
    """The exfiltration path: any readable file, declared as an image."""
    key = tmp_path / "id_rsa"
    key.write_bytes(PEM_KEY)
    out = _payload(await session.call_tool("upload_media", {"path": str(key), "mime": "image/png"}))
    assert out["ok"] is False and out["code"] == "invalid_media"
    assert "outside the allowed media roots" in out["message"]
    assert "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ" not in json.dumps(out)
    assert fake_x.requests == []


async def test_upload_media_refuses_pulsar_home_even_under_a_root(
    authed, store, fake_x, paths, tmp_path
):
    token_file = store.token_file
    rt = Runtime(paths, settings=Settings(media_roots=(tmp_path,)), transport=fake_x.transport())
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            out = _payload(await s.call_tool("upload_media", {"path": str(token_file)}))
    await rt.aclose()
    assert out["code"] == "invalid_media" and "pulsar's own state" in out["message"]
    assert fake_x.requests == []


async def test_upload_by_path_is_off_without_configured_roots(
    authed, fake_x, paths, tmp_path, monkeypatch
):
    """With no [media] roots the server's cwd (maybe / or $HOME) is not a default root."""
    (tmp_path / "pic.png").write_bytes(PNG_1PX)
    monkeypatch.chdir(tmp_path)
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            for path in ("pic.png", str(tmp_path / "pic.png")):
                out = _payload(await s.call_tool("upload_media", {"path": path}))
                assert out["code"] == "invalid_config" and "base64" in out["message"]
            assert fake_x.requests == []
            b64 = base64.b64encode(PNG_1PX).decode()
            ok = _payload(await s.call_tool("upload_media", {"base64": b64}))
            assert ok["ok"] is True
    await rt.aclose()


@pytest.mark.parametrize(
    ("name", "data", "args", "needle"),
    [
        ("id_rsa", PEM_KEY, {"mime": "image/png"}, "not a recognised"),
        ("photo.png", JPEG, {}, "does not match"),
        ("photo.jpg", JPEG, {"mime": "image/png"}, "does not match"),
        ("../escape.png", PNG_1PX, {}, "outside the allowed media roots"),
    ],
)
async def test_upload_media_path_refusals_never_reach_x(
    session, authed, fake_x, media_dir, name, data, args, needle
):
    target = media_dir / name
    target.write_bytes(data)
    out = _payload(await session.call_tool("upload_media", {"path": str(target), **args}))
    assert out["ok"] is False and out["code"] == "invalid_media" and needle in out["message"]
    assert fake_x.requests == []


async def test_upload_media_refuses_symlink_out_of_the_root(
    session, authed, fake_x, media_dir, tmp_path
):
    (tmp_path / "secret.png").write_bytes(PNG_1PX)
    (media_dir / "link.png").symlink_to(tmp_path / "secret.png")
    out = _payload(await session.call_tool("upload_media", {"path": "link.png"}))
    assert out["code"] == "invalid_media" and "outside" in out["message"]
    assert fake_x.requests == []


async def test_upload_media_honours_configured_roots(paths, fake_x, authed, tmp_path):
    root = tmp_path / "marketing"
    root.mkdir()
    (root / "pic.png").write_bytes(PNG_1PX)
    rt = Runtime(paths, settings=Settings(media_roots=(root,)), transport=fake_x.transport())
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            ok = _payload(await s.call_tool("upload_media", {"path": str(root / "pic.png")}))
            assert ok["ok"] is True and ok["mime"] == "image/png"
            fake_x.requests.clear()
            (tmp_path / "elsewhere.png").write_bytes(PNG_1PX)
            out = _payload(
                await s.call_tool("upload_media", {"path": str(tmp_path / "elsewhere.png")})
            )
            assert out["code"] == "invalid_media" and str(root) in out["detail"]["roots"]
            assert fake_x.requests == []
    await rt.aclose()


async def test_upload_media_base64_with_mismatched_mime_never_reaches_x(session, authed, fake_x):
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(JPEG).decode(), "mime": "image/png"}
        )
    )
    assert out["code"] == "invalid_media"
    assert out["detail"] == {"declared": "image/png", "sniffed": "image/jpeg"}
    assert fake_x.requests == []


async def test_delete_post(session, authed, fake_x, paths):
    out = _payload(await session.call_tool("delete_post", {"post_id": "101"}))
    assert out == {"ok": True, "post_id": "101", "deleted": True}
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["tool"] == "delete_post" and line["post_id"] == "101"


@pytest.mark.parametrize(
    "post_id", ["../users/1/retweets/555", "101?x=1", "101#f", "abc", "", "1" * 20, "１０１"]
)
async def test_delete_post_refuses_anything_but_a_numeric_id(session, authed, fake_x, post_id):
    """A path segment in the id would steer DELETE to another endpoint (un-retweet, ...)."""
    out = _payload(await session.call_tool("delete_post", {"post_id": post_id}))
    assert out["ok"] is False and out["code"] == "invalid_argument"
    assert fake_x.requests == []


@pytest.mark.parametrize(
    "args",
    [
        {"reply_to_post_id": "../1"},
        {"quote_post_id": "1/likes"},
        {"media_ids": ["710000", "7?x"]},
    ],
)
async def test_create_post_refuses_non_numeric_ids(session, authed, fake_x, paths, args):
    out = _payload(await session.call_tool("create_post", {"text": "hi", **args}))
    assert out["ok"] is False and out["code"] == "invalid_argument"
    assert fake_x.requests == [] and not paths.ledger_db.exists()
    if "media_ids" not in args:
        out = _payload(await session.call_tool("validate_post", {"text": "hi", **args}))
        assert out["code"] == "invalid_argument"


@pytest.mark.parametrize(
    "token_response",
    [
        lambda: httpx.Response(200, text="<html>maintenance</html>"),
        lambda: httpx.Response(200, json={"token_type": "bearer"}),
    ],
)
async def test_unreadable_refresh_is_a_failed_write_not_outcome_unknown(
    paths, store, bundle, fake_x, token_response
):
    """Only the token POST was sent, so the post certainly did not happen."""
    import time

    bundle.expires_at = time.time() - 10
    register(paths, bundle, handle="constworks", provider_user_id="1234567890")

    def handle(request):
        if request.url.path.endswith("/oauth2/token"):
            fake_x.requests.append(request)
            return token_response()
        return fake_x.handle(request)

    rt = Runtime(paths, settings=Settings(), transport=httpx.MockTransport(handle))
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            out = _payload(
                await s.call_tool("create_post", {"text": "hello", "idempotency_key": "k1"})
            )
    await rt.aclose()
    assert out["code"] == "api_error" and "token refresh failed" in out["message"]
    assert fake_x.calls("POST", "/tweets") == []
    assert rt.ledger.get("k1").state == "failed"


async def test_post_that_went_live_stays_ok_when_bookkeeping_fails(
    paths, authed, fake_x, monkeypatch, caplog
):
    from pulsar.core.errors import INSECURE_STORAGE, PulsarError

    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())

    def broken_publish(*_a, **_k):
        raise PulsarError(INSECURE_STORAGE, "home went wide mid-flight")

    monkeypatch.setattr(rt.ledger, "item_published", broken_publish)
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            out = _payload(await s.call_tool("create_post", {"text": "hello"}))
    await rt.aclose()
    assert out["ok"] is True and out["post_id"] == "101"
    assert "101" in caplog.text, "the live post id must be recoverable from the log"


async def test_unexpected_exceptions_become_results_not_tracebacks(
    paths, authed, fake_x, monkeypatch
):
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())

    async def broken_me():
        raise KeyError("data")

    monkeypatch.setattr(rt.client, "me", broken_me)
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            out = _payload(await s.call_tool("whoami", {}))
    await rt.aclose()
    assert out == {
        "ok": False,
        "code": "api_error",
        "message": "internal error: KeyError",
        "retryable": False,
    }


@pytest.mark.parametrize(
    "row",
    [
        {"handle": "constworks"},  # no user id
        {"provider_user_id": "1234567890"},  # no handle
        {"handle": "constworks", "provider_user_id": "1", "binding_id": "another-login"},
    ],
)
async def test_incomplete_or_foreign_identity_is_looked_up(session, fake_x, paths, bundle, row):
    register(paths, bundle, **row)
    out = _payload(await session.call_tool("whoami", {}))
    assert out["username"] == "constworks" and fake_x.calls("GET", "/users/me")


async def test_identity_looked_up_before_a_relogin_is_not_trusted_after_it(
    paths, store, bundle, fake_x
):
    """A whoami that raced `auth login` writes the old account under the old binding."""
    registry = AccountRegistry(paths)
    register(paths)
    old = store.rebind(bundle)
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())
    stale = Identity(provider_user_id="1", handle="old-account")
    registry.mark_verified(ALIAS, stale, old.binding_id)
    assert (await rt.whoami())["username"] == "old-account"
    new = TokenBundle(**{**bundle.__dict__, "binding_id": None})
    store.rebind(new)
    # the racing lookup lands after the re-login
    registry.mark_verified(ALIAS, stale, old.binding_id)
    assert (await rt.whoami())["username"] == "constworks"
    assert (await rt.whoami())["username"] == "constworks"
    assert len(fake_x.calls("GET", "/users/me")) == 1
    await rt.aclose()


async def test_create_post_on_a_skipped_key_is_a_conflict_not_a_receipt(
    session, authed, fake_x, paths
):
    from pulsar.core.ledger import AccountRef, Ledger

    ref = AccountRef(alias=ALIAS, provider="x")
    Ledger(paths).skip(key="never", provider="x", account=ref, caller="op", note="dropped")
    out = _payload(
        await session.call_tool("create_post", {"text": "hi", "idempotency_key": "never"})
    )
    assert out["ok"] is False and out["code"] == "idempotency_conflict"
    assert out["detail"]["state"] == "skipped"
    assert fake_x.calls("POST", "/tweets") == []
