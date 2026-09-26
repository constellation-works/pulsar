"""``pulsar auth``: status per account (cached / --live / --offline), login, logout, migrate."""

import json
import time

import pytest

from pulsar.core.accounts import AccountRegistry
from pulsar.core.store import TokenBundle
from pulsar.providers.x import auth as x_auth
from pulsar.surfaces.cli import main, status_report

from .conftest import ALIAS, ROTATED_ACCESS, SECRETS, register

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


async def test_default_reads_the_cache_and_says_refresh_is_unproven(paths, fake_x):
    expired = TokenBundle(
        access_token="old", refresh_token="r", expires_at=time.time() - 60, scope="s", client_id="c"
    )
    _verified(paths, expired)
    out, code = await status_report(paths, transport=fake_x.transport())
    assert fake_x.requests == [], "the cached path must not touch X"
    [entry] = out["accounts"]
    assert entry["alias"] == ALIAS and entry["status"] == "active"
    assert entry["account_source"] == "cache" and entry["verified"] is False
    assert entry["token_state"] == "expired"
    assert "--live" in entry["note"] and "expired" in entry["note"]
    assert entry["mismatch"] is False and entry["healthy"] is True
    assert code == 0


async def test_live_forces_a_refresh_and_bypasses_a_stale_cache(paths, bundle, store, fake_x):
    _verified(paths, bundle, handle="someone-else")
    out, code = await status_report(paths, live=True, transport=fake_x.transport())
    assert code == 0
    [entry] = out["accounts"]
    assert entry["refreshed"] is True and entry["verified"] is True
    assert entry["account"] == {"user_id": fake_x.user_id, "username": "constworks"}
    assert entry["account_source"] == "live" and entry["whoami_cache"] == "refreshed"
    assert len(fake_x.calls("POST", "/oauth2/token")) == 1
    assert store.load().access_token == ROTATED_ACCESS, "rotated pair was saved"
    assert AccountRegistry(paths).get(ALIAS).handle == "constworks"
    assert "note" not in entry
    assert not any(s in json.dumps(out) for s in SECRETS)


async def test_live_reports_a_dead_refresh_token_as_reauth(paths, authed, fake_x):
    fake_x.refresh_status = 400
    out, code = await status_report(paths, live=True, transport=fake_x.transport())
    assert code == 1
    [entry] = out["accounts"]
    assert entry["reauth_required"] is True and entry["verified"] is False
    assert entry["error"]["code"] == "auth_expired"
    assert entry["status"] == "reauth_required", "the registry remembers it"


async def test_live_lifts_reauth_required_once_the_refresh_works(paths, bundle, fake_x):
    _verified(paths, bundle, status="reauth_required")
    out, code = await status_report(paths, offline=True)
    assert code == 1 and out["accounts"][0]["reauth_required"] is True
    out, code = await status_report(paths, live=True, transport=fake_x.transport())
    assert code == 0 and out["accounts"][0]["status"] == "active"
    assert AccountRegistry(paths).get(ALIAS).status == "active"


async def test_offline_makes_no_request(paths, bundle, fake_x):
    _verified(paths, bundle)
    out, code = await status_report(paths, offline=True, transport=fake_x.transport())
    assert fake_x.requests == []
    [entry] = out["accounts"]
    assert entry["account"]["username"] == "constworks" and entry["account_source"] == "cache"
    assert code == 0


async def test_unauthorized_home(paths, fake_x):
    out, code = await status_report(paths, transport=fake_x.transport())
    assert out["accounts"] == [] and code == 1
    assert fake_x.requests == []


def test_live_and_offline_are_mutually_exclusive(paths):
    with pytest.raises(SystemExit):
        main(["auth", "status", "--live", "--offline"])


# -- several accounts -------------------------------------------------------------


async def test_status_over_two_accounts_one_needing_reauth_exits_1(paths, bundle, fake_x):
    _verified(paths, bundle)
    _verified(paths, _other_bundle(bundle), "x:other", "other", "2", status="reauth_required")
    out, code = await status_report(paths, transport=fake_x.transport())
    assert code == 1
    by_alias = {e["alias"]: e for e in out["accounts"]}
    assert list(by_alias) == ["x:constworks", "x:other"]
    assert by_alias["x:constworks"]["healthy"] is True
    other = by_alias["x:other"]
    assert other["healthy"] is False and other["reauth_required"] is True
    assert other["status"] == "reauth_required"
    assert other["account"] == {"user_id": "2", "username": "other"}

    out, code = await status_report(paths, account="X:@ConstWorks", transport=fake_x.transport())
    assert code == 0 and [e["alias"] for e in out["accounts"]] == ["x:constworks"]


async def test_status_flags_a_bound_handle_that_is_not_the_expected_one(paths, authed, fake_x):
    _config(paths, '[accounts."x:constworks"]\nexpected_handle = "constworks"\n')
    fake_x.username = "impostor"
    out, code = await status_report(paths, transport=fake_x.transport())
    [entry] = out["accounts"]
    assert entry["account"]["username"] == "impostor"
    assert entry["mismatch"] is True and entry["healthy"] is False
    assert entry["expected_handle"] == "constworks"
    assert code == 1


async def test_status_of_an_unknown_account(paths, bundle):
    _verified(paths, bundle)
    out, code = await status_report(paths, account="x:nobody", offline=True)
    assert code == 1 and out["error"]["code"] == "unknown_account"
    assert "x:constworks" in out["error"]["message"]


# -- login / logout / migrate -------------------------------------------------------


@pytest.fixture
def fake_login(monkeypatch, bundle, fake_x):
    """Skip the browser: the code exchange yields ``bundle``; /users/me is the fake X."""
    real_fetch = x_auth.fetch_identity
    monkeypatch.setattr(x_auth, "authorize", lambda client_id, **_: bundle)
    monkeypatch.setattr(
        x_auth,
        "fetch_identity",
        lambda b, transport=None: real_fetch(b, transport=fake_x.transport()),
    )
    return fake_x


def _login(alias="x:constworks"):
    return main(["auth", "login", "--account", alias, "--client-id", "cid", "--no-browser"])


def test_login_binds_the_named_account_after_asking_x(paths, store, fake_login, capsys):
    assert _login() == 0
    row = AccountRegistry(paths).get(ALIAS)
    assert (row.handle, row.provider_user_id, row.status) == ("constworks", "1234567890", "active")
    assert row.binding_id and row.binding_id == store.load().binding_id
    assert row.bound_at and row.verified_at
    out = capsys.readouterr().out
    assert json.loads(out)["verified"] is True
    assert not any(s in out for s in SECRETS)
    assert fake_login.calls("GET", "/users/me")


def test_login_refuses_a_token_for_another_handle_and_stores_nothing(
    paths, store, fake_login, capsys
):
    fake_login.username = "someoneelse"
    assert _login() == 1
    err = capsys.readouterr().err
    assert "account_mismatch" in err and "@constworks" in err and "@someoneelse" in err
    assert not store.token_file.exists()
    assert AccountRegistry(paths).accounts() == {}
    assert not paths.client_file.exists(), "a refused login leaves nothing behind"


def test_login_needs_an_account(paths, fake_login, capsys):
    assert main(["auth", "login", "--client-id", "cid", "--no-browser"]) == 2
    assert "--account" in capsys.readouterr().err


def test_logout_deletes_the_tokens_and_keeps_the_row_as_revoked(paths, bundle, store, capsys):
    _verified(paths, bundle)
    assert main(["auth", "logout", "--account", "x:constworks"]) == 0
    assert store.load() is None
    row = AccountRegistry(paths).get(ALIAS)
    assert row.status == "revoked" and row.handle == "constworks"


async def test_a_revoked_account_is_not_healthy(paths, bundle):
    _verified(paths, bundle)
    AccountRegistry(paths).logout(ALIAS)
    out, code = await status_report(paths, offline=True)
    assert code == 1 and out["accounts"][0]["status"] == "revoked"


async def test_migrate_names_legacy_credentials(paths, bundle, legacy_store, store, capsys):
    legacy_store.save(bundle)
    out, code = await status_report(paths, offline=True)
    assert code == 1 and "pulsar auth migrate --account" in out["legacy"]
    assert main(["auth", "migrate", "--account", "x:constworks"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "migrated"
    assert store.load() == bundle and not paths.token_file.exists()
    assert main(["auth", "migrate", "--account", "x:constworks"]) == 0  # idempotent
