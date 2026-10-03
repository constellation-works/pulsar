"""The composition root's own contracts: log redaction, the caller label and the
provider -> channel factory."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path

import httpx
import pytest

from pulsar.app import tools
from pulsar.app.core.account import FernetFileStore
from pulsar.app.core.channels.bluesky import BlueskyChannel
from pulsar.app.core.channels.x import XChannel
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.runtime import CALLER_ENV, RedactingFilter, configure_logging
from pulsar.app.settings import Settings
from pulsar.internal.errors import AuthExpired, PulsarError

from .conftest import ALIAS, FakeX, make_app, make_runtime, register
from .fake_bsky import DID, HANDLE, FakeBsky

LEAKED_VALUE = "abcdefghijklmnopqrstuvwxyz0123456789"
LEAKED = f"Authorization: Bearer {LEAKED_VALUE}"


@pytest.fixture
def captured() -> tuple[logging.Logger, io.StringIO]:
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.addFilter(RedactingFilter())
    logger = logging.getLogger("pulsar.test.redaction")
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, sink
    logger.removeHandler(handler)


def test_the_log_filter_masks_messages_and_their_arguments(captured):
    logger, sink = captured
    logger.warning(LEAKED)
    logger.warning("the provider answered %r", {"echo": LEAKED})
    out = sink.getvalue()
    assert LEAKED_VALUE not in out
    assert out.count("[redacted:bearer header]") == 2


def test_the_log_filter_masks_tracebacks(captured):
    logger, sink = captured
    try:
        raise RuntimeError(f"request failed: {LEAKED}")
    except RuntimeError:
        logger.exception("upload failed")
    out = sink.getvalue()
    assert "Traceback" in out and "upload failed" in out
    assert LEAKED_VALUE not in out


def test_configure_logging_installs_one_redacting_stderr_handler(monkeypatch, capsys):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.NOTSET)
    configure_logging()
    configure_logging()
    (handler,) = root.handlers
    assert any(isinstance(f, RedactingFilter) for f in handler.filters)
    logging.getLogger("pulsar.test").warning(LEAKED)
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout carries the payload
    assert "[redacted:bearer header]" in captured.err and LEAKED_VALUE not in captured.err


def test_the_caller_label_prefers_the_argument_then_the_environment(paths, monkeypatch):
    runtime = make_runtime(paths)
    assert runtime.caller(None) == "unknown"
    monkeypatch.setenv(CALLER_ENV, "nightly-routine")  # read live, after construction
    assert runtime.caller(None) == "nightly-routine"
    assert runtime.caller("release-bot") == "release-bot"


def test_a_credential_shaped_caller_is_refused(paths):
    with pytest.raises(PulsarError) as exc:
        make_runtime(paths).caller(f"bot {LEAKED}")
    assert exc.value.code == "secret_detected"
    assert LEAKED_VALUE not in str(exc.value)


def _off_the_loop(calls: list[str], name: str, fn):
    """``fn``, recording ``name`` and failing if it runs on the event loop's thread."""

    def wrapper(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            calls.append(name)
            return fn(*args, **kwargs)
        raise AssertionError(f"{name} ran on the event loop")

    return wrapper


@pytest.mark.anyio
async def test_account_and_credential_io_runs_off_the_event_loop(
    paths, authed, fake_x, monkeypatch
):
    rt = make_runtime(paths, settings=Settings(), transport=fake_x.transport())
    calls: list[str] = []
    monkeypatch.setattr(rt, "account", _off_the_loop(calls, "account", rt.account))
    registry = rt.registry
    for name in ("mark_verified", "mark_status"):
        monkeypatch.setattr(registry, name, _off_the_loop(calls, name, getattr(registry, name)))
    load = FernetFileStore.load
    monkeypatch.setattr(
        FernetFileStore, "load", lambda self: _off_the_loop(calls, "load", load)(self)
    )
    try:
        await rt.identity()  # unverified: asks X, then records the identity
        fake_x.fail_auth_once, fake_x.refresh_status = True, 401
        with pytest.raises(AuthExpired):
            await rt.identity(live=True)
    finally:
        await rt.aclose()
    assert {"account", "load", "mark_verified", "mark_status"} <= set(calls)


@pytest.mark.parametrize("status", ["active", "revoked"])
@pytest.mark.anyio
async def test_a_missing_login_names_the_home_to_log_in_to(paths, status):
    """Under Orbit the home is the plugin's, so a bare ``pulsar auth login``
    would bind the operator's default home instead."""
    register(paths, None, status=status)
    rt = make_runtime(paths, settings=Settings())
    try:
        with pytest.raises(AuthExpired) as exc:
            await rt.identity(ALIAS)
    finally:
        await rt.aclose()
    assert f"`PULSAR_HOME={paths.home} pulsar auth login --account {ALIAS}`" in exc.value.message


# -- the provider -> channel factory ------------------------------------------------

BSKY = "bsky:constworks.bsky.social"


def _both(fake_x: FakeX, fake_bsky: FakeBsky) -> httpx.MockTransport:
    """One network with X's API and a Bluesky PDS on it, routed by host."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "bsky.social":
            return fake_bsky.handle_request(request)
        return fake_x.handle(request)

    return httpx.MockTransport(route)


@pytest.mark.anyio
async def test_the_factory_builds_each_providers_channel(paths, bundle, fake_x):
    register(paths, bundle)
    register(paths, bundle, alias=BSKY)
    fake_bsky = FakeBsky()
    rt = make_runtime(paths, settings=Settings(), transport=_both(fake_x, fake_bsky))
    try:
        assert isinstance(rt.channel(ALIAS, user_id="1", handle="constworks"), XChannel)
        assert isinstance(rt.channel(BSKY, user_id=DID, handle=HANDLE), BlueskyChannel)
        account = await rt.identity(BSKY)  # asks the PDS, then records the identity
    finally:
        await rt.aclose()
    assert (account.provider_user_id, account.handle) == (DID, HANDLE)
    assert len(fake_bsky.calls("com.atproto.server.getSession")) == 1
    assert fake_x.requests == []


@pytest.mark.anyio
async def test_a_provider_without_a_channel_is_unsupported(paths, monkeypatch):
    rt = make_runtime(paths, settings=Settings())
    monkeypatch.setattr(rt.registry, "store", lambda alias: None)
    try:
        with pytest.raises(PulsarError) as exc:
            rt.client_for("mastodon:someone")
    finally:
        await rt.aclose()
    assert exc.value.code == "unsupported"


@pytest.mark.anyio
async def test_the_single_post_tools_are_xs(paths, bundle):
    register(paths, bundle, alias=BSKY)
    fake_bsky = FakeBsky()
    rt = make_runtime(paths, settings=Settings(), transport=fake_bsky.transport())
    try:
        with pytest.raises(PulsarError) as writer:
            await rt.writer(BSKY)
        with pytest.raises(PulsarError) as create:
            await tools.create_post(
                rt,
                text="hi",
                reply_to_post_id=None,
                quote_post_id=None,
                media_ids=None,
                dry_run=False,
                caller=None,
                idempotency_key=None,
                account=BSKY,
            )
    finally:
        await rt.aclose()
    assert writer.value.code == create.value.code == "unsupported"
    assert fake_bsky.calls("com.atproto.repo.createRecord") == []


PLAN = """\
accounts: [x:constworks, bsky:constworks.bsky.social]
posts:
  - text: "Orbit v0.26 is out, with the long X copy and its notes"
  - text: "Notes: https://example.com/notes"
variants:
  bsky:
    posts:
      - text: "Orbit v0.26 is out #orbit"
      - text: "Notes: https://example.com/notes"
"""


@pytest.mark.anyio
async def test_a_plan_with_variants_publishes_to_both_providers(
    paths, bundle, fake_x, tmp_path: Path
):
    register(paths, bundle)
    register(paths, bundle, alias=BSKY)
    fake_bsky = FakeBsky()
    plan = tmp_path / "plan.yaml"
    plan.write_text(PLAN)
    app = make_app(paths, transport=_both(fake_x, fake_bsky))
    out, code = await app.publish(plan, confirm=True)
    assert code == 0, out
    receipts = {r["account"]: r for r in out["results"]}
    assert set(receipts) == {ALIAS, BSKY}
    x_receipt, bsky_receipt = receipts[ALIAS], receipts[BSKY]
    assert x_receipt["ok"] and bsky_receipt["ok"]

    tweets = [json.loads(r.content) for r in fake_x.calls("POST", "/tweets")]
    assert [t["text"] for t in tweets] == [
        "Orbit v0.26 is out, with the long X copy and its notes",
        "Notes: https://example.com/notes",
    ]
    first, second = fake_bsky.posts()
    assert first["text"] == "Orbit v0.26 is out #orbit"
    assert first["facets"][0]["features"][0] == {
        "$type": "app.bsky.richtext.facet#tag",
        "tag": "orbit",
    }
    assert second["reply"]["root"]["uri"] == bsky_receipt["items"][0]["post_id"]
    assert [i["url"].split("/post/")[0] for i in bsky_receipt["items"]] == [
        f"https://bsky.app/profile/{HANDLE}"
    ] * 2

    rows = {r.account_alias: r for r in SqliteLedger(paths).history()}
    assert set(rows) == {ALIAS, BSKY}
    assert rows[ALIAS].provider == "x" and rows[BSKY].provider == "bsky"
    assert [i.state for i in rows[BSKY].items] == ["published", "published"]
    assert [i.post_id for i in rows[BSKY].items] == [i["post_id"] for i in bsky_receipt["items"]]
