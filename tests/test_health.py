"""Account health (``pulsar auth status``, ``pulsar.status``): three-valued, offline by
default, ``--live`` proves the binding."""

import json
import os
import time

import pytest

from pulsar.accounts import AccountRegistry, TokenBundle
from pulsar.app import attention
from pulsar.errors import PulsarError

from .conftest import ALIAS, ROTATED_ACCESS, SECRETS, make_app, register

pytestmark = pytest.mark.anyio


def _verified(paths, bundle, alias=ALIAS, handle="constworks", user_id="1", **row):
    return register(paths, bundle, alias, handle=handle, provider_user_id=user_id, **row)


def _other_bundle(bundle):
    return TokenBundle(
        **{**bundle.__dict__, "access_token": "access-other", "refresh_token": "r-o"}
    )


def _config(paths, text):
    paths.ensure()
    paths.settings_file.write_text(text)
    paths.settings_file.chmod(0o600)  # config.toml must not be group/world-writable


def _snapshot(root):
    """Every path under ``root`` with its size and mtime: what a read must not change."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            out[path] = (st.st_size, st.st_mtime_ns)
    return out


async def test_a_valid_cached_binding_is_healthy_without_asking_x(paths, bundle, fake_x):
    _verified(paths, bundle)
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status()
    assert fake_x.requests == [], "the default report never touches X"
    [entry] = out["accounts"]
    assert entry["health"] == "healthy" and entry["healthy"] is True and entry["reason"] is None
    assert entry["account_source"] == "cache" and entry["verified"] is False
    assert entry["mismatch"] is False and code == 0
    assert attention(entry, paths.home) is None


async def test_an_expired_token_whose_refresh_is_unproven_is_unverified(paths, fake_x):
    expired = TokenBundle(
        access_token="old", refresh_token="r", expires_at=time.time() - 60, scope="s", client_id="c"
    )
    _verified(paths, expired)
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status()
    assert fake_x.requests == []
    [entry] = out["accounts"]
    assert entry["token_state"] == "expired"
    assert entry["health"] == "unverified" and entry["healthy"] is False
    assert code == 1, "unknown is never reported as healthy"
    assert f"PULSAR_HOME={paths.home} pulsar auth status --live --account {ALIAS}" in attention(
        entry, paths.home
    )


async def test_a_binding_with_no_cached_identity_is_unverified(paths, authed, fake_x):
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status()
    [entry] = out["accounts"]
    assert entry["health"] == "unverified" and entry["mismatch"] is None and code == 1
    assert fake_x.requests == []


async def test_every_entry_has_every_key(paths, bundle, fake_x):
    _verified(paths, bundle)
    register(paths, None, "x:unbound")
    out, _ = await make_app(paths).auth_status()
    keys = [set(e) for e in out["accounts"]]
    assert keys[0] == keys[1], "absent values are null, never missing keys"
    assert set(out) == {"home", "client_id", "default_account", "legacy", "accounts"}


async def test_the_default_report_writes_nothing(paths, bundle):
    _verified(paths, bundle, status="reauth_required")
    before = _snapshot(paths.home)
    out, code = await make_app(paths).auth_status()
    assert code == 1 and out["accounts"][0]["health"] == "unhealthy"
    assert _snapshot(paths.home) == before


async def test_live_forces_a_refresh_and_bypasses_a_stale_cache(paths, bundle, store, fake_x):
    _verified(paths, bundle, handle="someone-else")
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status(live=True)
    assert code == 0
    [entry] = out["accounts"]
    assert entry["refreshed"] is True and entry["verified"] is True
    assert entry["account"] == {"user_id": fake_x.user_id, "username": "constworks"}
    assert entry["account_source"] == "live" and entry["health"] == "healthy"
    assert len(fake_x.calls("POST", "/oauth2/token")) == 1
    assert store.load().access_token == ROTATED_ACCESS, "rotated pair was saved"
    assert AccountRegistry(paths).get(ALIAS).handle == "constworks"
    assert not any(s in json.dumps(out) for s in SECRETS)


async def test_live_reports_a_dead_refresh_token_as_reauth(paths, authed, fake_x):
    fake_x.refresh_status = 400
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status(live=True)
    assert code == 1
    [entry] = out["accounts"]
    assert entry["reauth_required"] is True and entry["verified"] is False
    assert entry["error"]["code"] == "auth_expired" and entry["health"] == "unhealthy"
    assert entry["status"] == "reauth_required", "the registry remembers it"


async def test_a_live_check_that_could_not_run_is_unverified_not_unhealthy(paths, bundle, fake_x):
    _verified(paths, bundle, user_id="1234567890")
    fake_x.refresh_status = 503  # X is down: nothing is learned about the binding
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status(live=True)
    [entry] = out["accounts"]
    assert entry["error"]["retryable"] is True
    assert entry["health"] == "unverified" and "could not run" in entry["reason"]
    assert entry["reauth_required"] is False and code == 1


async def test_live_lifts_reauth_required_once_the_refresh_works(paths, bundle, fake_x):
    _verified(paths, bundle, status="reauth_required")
    out, code = await make_app(paths).auth_status()
    assert code == 1 and out["accounts"][0]["reauth_required"] is True
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status(live=True)
    assert code == 0 and out["accounts"][0]["status"] == "active"
    assert AccountRegistry(paths).get(ALIAS).status == "active"


async def test_an_empty_home_is_not_healthy(paths, fake_x):
    out, code = await make_app(paths, transport=fake_x.transport()).auth_status()
    assert out["accounts"] == [] and code == 1
    assert fake_x.requests == []


async def test_two_accounts_one_needing_reauth_exits_1(paths, bundle):
    _verified(paths, bundle)
    _verified(paths, _other_bundle(bundle), "x:other", "other", "2", status="reauth_required")
    out, code = await make_app(paths).auth_status()
    assert code == 1
    by_alias = {e["alias"]: e for e in out["accounts"]}
    assert list(by_alias) == ["x:constworks", "x:other"]
    assert by_alias["x:constworks"]["health"] == "healthy"
    other = by_alias["x:other"]
    assert other["health"] == "unhealthy" and other["reauth_required"] is True
    assert other["account"] == {"user_id": "2", "username": "other"}
    assert f"PULSAR_HOME={paths.home} pulsar auth login --account x:other" in attention(
        other, paths.home
    ), "the remedy names the home it applies to"

    out, code = await make_app(paths).auth_status(account="X:@ConstWorks")
    assert code == 0 and [e["alias"] for e in out["accounts"]] == ["x:constworks"]


async def test_a_bound_handle_that_is_not_the_expected_one_is_unhealthy(paths, bundle):
    _config(paths, '[accounts."x:constworks"]\nexpected_handle = "constworks"\n')
    _verified(paths, bundle, handle="impostor")
    out, code = await make_app(paths).auth_status()
    [entry] = out["accounts"]
    assert entry["account"]["username"] == "impostor"
    assert entry["mismatch"] is True and entry["health"] == "unhealthy"
    assert entry["expected_handle"] == "constworks" and code == 1


async def test_an_unknown_account_is_an_error(paths, bundle):
    _verified(paths, bundle)
    with pytest.raises(PulsarError) as exc:
        await make_app(paths).auth_status(account="x:nobody")
    assert exc.value.code == "unknown_account"
    assert "x:constworks" in exc.value.message


async def test_a_revoked_account_is_unhealthy(paths, bundle):
    _verified(paths, bundle)
    AccountRegistry(paths).logout(ALIAS)
    out, code = await make_app(paths).auth_status()
    [entry] = out["accounts"]
    assert code == 1 and entry["status"] == "revoked" and entry["health"] == "unhealthy"


async def test_legacy_credentials_are_reported_not_migrated(paths, bundle, legacy_store):
    legacy_store.save(bundle)
    out, code = await make_app(paths).auth_status()
    assert code == 1 and out["legacy"] is not None and out["legacy"]["state"] != "none"
    assert paths.token_file.exists(), "a report never migrates"
    assert AccountRegistry(paths).accounts() == {}
