"""``pulsar auth status``: cached by default, proven with --live, silent with --offline."""

import json
import time

import pytest

from pulsar.cli import main, status_report
from pulsar.store import TokenBundle

from .conftest import ROTATED_ACCESS, SECRETS

pytestmark = pytest.mark.anyio


def _cache(paths, username="constworks"):
    paths.ensure()
    paths.whoami_cache.write_text(json.dumps({"user_id": "1", "username": username}))


async def test_default_reads_the_cache_and_says_refresh_is_unproven(paths, store, fake_x):
    store.save(
        TokenBundle(
            access_token="old",
            refresh_token="r",
            expires_at=time.time() - 60,
            scope="s",
            client_id="c",
        )
    )
    _cache(paths)
    out, code = await status_report(paths, transport=fake_x.transport())
    assert fake_x.requests == [], "the cached path must not touch X"
    assert out["account_source"] == "cache" and out["verified"] is False
    assert out["token_state"] == "expired"
    assert "--live" in out["note"] and "expired" in out["note"]
    assert code == 0


async def test_live_forces_a_refresh_and_bypasses_a_stale_cache(paths, authed, store, fake_x):
    _cache(paths, username="someone-else")
    out, code = await status_report(paths, live=True, transport=fake_x.transport())
    assert code == 0
    assert out["refreshed"] is True and out["verified"] is True
    assert out["account"] == {"user_id": fake_x.user_id, "username": "constworks"}
    assert out["account_source"] == "live" and out["whoami_cache"] == "refreshed"
    assert len(fake_x.calls("POST", "/oauth2/token")) == 1
    assert store.load().access_token == ROTATED_ACCESS, "rotated pair was saved"
    assert json.loads(paths.whoami_cache.read_text())["username"] == "constworks"
    assert "note" not in out
    blob = json.dumps(out)
    assert not any(s in blob for s in SECRETS)


async def test_live_reports_a_dead_refresh_token_as_reauth(paths, authed, fake_x):
    fake_x.refresh_status = 400
    out, code = await status_report(paths, live=True, transport=fake_x.transport())
    assert code == 1
    assert out["reauth_required"] is True and out["verified"] is False
    assert out["error"]["code"] == "auth_expired"


async def test_offline_makes_no_request(paths, authed, fake_x):
    _cache(paths)
    out, code = await status_report(paths, offline=True, transport=fake_x.transport())
    assert fake_x.requests == []
    assert out["account"]["username"] == "constworks" and out["account_source"] == "cache"
    assert code == 0


async def test_unauthorized_home(paths, fake_x):
    out, code = await status_report(paths, transport=fake_x.transport())
    assert out["authorized"] is False and code == 1
    assert fake_x.requests == []


def test_live_and_offline_are_mutually_exclusive(paths):
    with pytest.raises(SystemExit):
        main(["auth", "status", "--live", "--offline"])
