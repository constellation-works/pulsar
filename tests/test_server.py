"""End-to-end through a real MCP client session over the in-memory transport."""

import base64
import json
import re

import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from pulsar.config import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES
from pulsar.server import TOOL_NAMES, Runtime, build_server

from .conftest import SECRETS

SECRET_PARAM = re.compile(r"(?i)token|secret|bearer|password|api[_-]?key|client[_-]?secret")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def session(paths, fake_x):
    rt = Runtime(paths, transport=fake_x.transport())
    server = build_server(rt)
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            yield s
    await rt.client.aclose()


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
    assert json.loads(paths.whoami_cache.read_text())["username"] == "constworks"
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
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["dry_run"] is True and line["post_id"] is None and line["caller"] == "test"


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
    assert out == {
        "ok": False,
        "code": "auth_expired",
        "message": "X authorization expired; a human must re-run `pulsar auth login`",
    }


async def test_upload_media_by_path_then_attach(session, authed, fake_x, tmp_path):
    png = tmp_path / "pic.png"
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


async def test_upload_video_by_path_and_log(session, authed, fake_x, paths, tmp_path):
    video = tmp_path / "reel.mp4"
    video.write_bytes(b"distinct-video-payload")
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
    data = b"mp4-payload"
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
            "upload_media", {"base64": base64.b64encode(b"video").decode(), "mime": "video/mp4"}
        )
    )
    assert out["ok"] is False and out["code"] == "invalid_media"
    assert "media_id" not in out
    assert out["detail"]["error"]["message"] == "Unsupported codec"
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["processing_state"] == "failed"
    assert line["mime"] == "video/mp4" and line["bytes"] == 5
    assert "media_id" not in line


async def test_upload_video_timeout_has_no_media_id_and_logs_state(
    session, authed, fake_x, paths, monkeypatch
):
    monkeypatch.setattr("pulsar.xapi.PROCESSING_TIMEOUT_SECONDS", 0)
    fake_x.media_finalize_info = {"state": "pending", "check_after_secs": 2}
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(b"video").decode(), "mime": "video/mp4"}
        )
    )
    assert out["code"] == "invalid_media" and "media_id" not in out
    assert out["detail"]["state"] == "pending"
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["processing_state"] == "timed_out"


async def test_upload_media_scans_bytes_before_x_write(session, authed, fake_x):
    data = b"prefix sk-abcdefghijklmnopqrstuvwxyz suffix"
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(data).decode(), "mime": "video/mp4"}
        )
    )
    assert out["code"] == "secret_detected"
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in json.dumps(out)
    assert fake_x.requests == []


async def test_upload_media_limits_and_mime(session, authed, fake_x, tmp_path):
    for name, size, needle in (
        ("clip.webm", 1, "unsupported media type video/webm"),
        ("large.mp4", MAX_VIDEO_BYTES + 1, str(MAX_VIDEO_BYTES)),
        ("large.png", MAX_IMAGE_BYTES + 1, str(MAX_IMAGE_BYTES)),
    ):
        media = tmp_path / name
        with media.open("wb") as fh:
            fh.truncate(size)
        out = _payload(await session.call_tool("upload_media", {"path": str(media)}))
        assert out["ok"] is False and out["code"] == "invalid_media"
        assert needle in out["message"]
    assert fake_x.requests == []


async def test_upload_video_base64_limit(session, authed, fake_x, monkeypatch):
    monkeypatch.setattr("pulsar.server.MAX_VIDEO_BYTES", 4)
    out = _payload(
        await session.call_tool(
            "upload_media", {"base64": base64.b64encode(b"12345").decode(), "mime": "video/mp4"}
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
async def test_upload_media_rejections(session, authed, args, needle):
    out = _payload(await session.call_tool("upload_media", args))
    assert out["ok"] is False and out["code"] == "invalid_media" and needle in out["message"]


async def test_delete_post(session, authed, fake_x, paths):
    out = _payload(await session.call_tool("delete_post", {"post_id": "101"}))
    assert out == {"ok": True, "post_id": "101", "deleted": True}
    line = json.loads(paths.write_log.read_text().splitlines()[-1])
    assert line["tool"] == "delete_post" and line["post_id"] == "101"
