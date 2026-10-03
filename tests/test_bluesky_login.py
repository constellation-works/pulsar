"""Bluesky login: atproto OAuth (PAR + PKCE + DPoP) against a fake identity
network, PDS and authorization server; refresh under the account's lock; logout."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from pulsar.app.core.account import (
    AccountConfig,
    AccountRegistry,
    FernetFileStore,
    load_client_id,
)
from pulsar.app.core.channels.bluesky import BlueskyLogin, check_client_id, loopback_client_id
from pulsar.app.core.channels.bluesky import auth as bsky_auth
from pulsar.app.core.channels.bluesky.dpop import Es256Proof
from pulsar.app.login import login
from pulsar.app.settings import Settings
from pulsar.internal.errors import AuthExpired, PulsarError
from pulsar.internal.guard import scan_for_secrets

from .conftest import (
    ACCESS,
    REFRESH,
    ROTATED_ACCESS,
    ROTATED_REFRESH,
    SECRETS,
    make_app,
    make_runtime,
    register,
)
from .fake_atproto import (
    AS_NONCE,
    EVE_DID,
    ISSUER,
    OTHER_ISSUER,
    PDS,
    PDS_NONCE,
    SCOPE,
    Browser,
    FakeAtproto,
    thumbprint,
    verify_proof,
)
from .fake_bsky import ALICE_DID, DID, HANDLE
from .lock_probe import blocked_on_lock

pytestmark = pytest.mark.anyio

BSKY = f"bsky:{HANDLE}"


@pytest.fixture
def fake() -> FakeAtproto:
    return FakeAtproto()


@pytest.fixture
def browser(fake, monkeypatch) -> Browser:
    """The human at the browser; the login's loopback listener is the fake redirect."""
    human = Browser(fake)
    monkeypatch.setattr(bsky_auth, "CallbackServer", human.redirect)
    return human


def _login(paths, fake, browser, *, settings=None, client_id=None, alias=BSKY):
    return login(
        paths,
        settings or Settings(),
        alias,
        client_id or loopback_client_id(),
        open_browser=False,
        transport=fake.transport(),
        notify=browser.notify,
    )


def _store(paths) -> FernetFileStore:
    return FernetFileStore.for_account(paths, BSKY)


def _nothing_stored(paths) -> None:
    assert not _store(paths).token_file.exists()
    assert AccountRegistry(paths).accounts() == {}
    assert not paths.client_file.exists(), "a refused login leaves nothing behind"


# -- the login ------------------------------------------------------------------------


def test_login_pushes_a_pkce_request_under_dpop_and_binds_the_verified_account(
    paths, fake, browser
):
    fake.next_nonce = "as-nonce-rotated"  # the server moves on while the human approves
    account = _login(paths, fake, browser)

    # PAR: PKCE S256, the scope, the loopback redirect the client id names, a login hint.
    [par] = fake.pars.values()
    assert par["code_challenge_method"] == "S256" and len(par["code_challenge"]) == 43
    assert par["scope"] == SCOPE and par["login_hint"] == HANDLE
    assert par["redirect_uri"] == "http://127.0.0.1:8976/callback"
    assert par["state"] == browser.states[0]
    # The human was sent to the authorization endpoint with only the client id and request URI.
    consent = urlsplit(browser.url)
    assert f"{consent.scheme}://{consent.netloc}{consent.path}" == f"{ISSUER}/oauth/authorize"
    assert set(parse_qs(consent.query)) == {"client_id", "request_uri"}

    # Both requests to the authorization server met a nonce challenge and were repeated once.
    assert fake.nonce_challenges == ["/oauth/par", "/oauth/token"]
    assert len(fake.posts("/oauth/par")) == 2 and len(fake.posts("/oauth/token")) == 2
    pars = [verify_proof(r.headers["DPoP"])[1] for r in fake.posts("/oauth/par")]
    assert "nonce" not in pars[0] and pars[1]["nonce"] == AS_NONCE
    proofs = [verify_proof(r.headers["DPoP"]) for r in fake.posts("/oauth/token")]
    assert proofs[0][1]["nonce"] == AS_NONCE, "the token request reuses the PAR's nonce"
    assert proofs[1][1]["nonce"] == "as-nonce-rotated"
    # One key for the whole login: the token is bound to the key the PAR was signed with.
    assert fake.bound_jkt == par["jkt"] == thumbprint(proofs[1][0]["jwk"])

    assert (account.alias, account.handle, account.provider_user_id) == (BSKY, HANDLE, DID)
    assert account.status == "active" and account.scopes == ("atproto", "transition:generic")
    stored = _store(paths).load()
    assert stored is not None and stored.binding_id == account.binding_id
    assert (stored.access_token, stored.refresh_token) == (ACCESS, REFRESH)
    assert stored.token_type == "DPoP"
    assert (stored.service, stored.token_url) == (PDS, f"{ISSUER}/oauth/token")
    assert thumbprint(Es256Proof.from_stored(stored.dpop_key).public_jwk) == fake.bound_jkt
    assert load_client_id(paths, "bsky") == loopback_client_id()
    assert browser.fake.codes == {}, "the code was spent"


def test_the_dpop_key_is_stored_encrypted_with_the_tokens(paths, fake, browser):
    _login(paths, fake, browser)
    store = _store(paths)
    key = store.load().dpop_key
    assert key and len(key) == 43
    raw = store.token_file.read_bytes()
    for secret in (*SECRETS, key):
        assert secret.encode() not in raw
    assert store.token_file.stat().st_mode & 0o777 == 0o600
    assert scan_for_secrets(f"leaked {key}"), "the key is masked like a token"


def test_a_token_for_another_account_is_refused_before_anything_is_stored(paths, fake, browser):
    fake.sub = ALICE_DID  # the human approved as @alice
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser)
    assert exc.value.code == "account_mismatch"
    assert f"@{HANDLE}" in exc.value.message and "@alice.bsky.social" in exc.value.message
    _nothing_stored(paths)


def test_a_handle_that_does_not_resolve_back_to_the_token_is_refused(paths, fake, browser):
    # Alice's DID document claims our handle, but the handle resolves to our DID, not hers.
    fake.sub = ALICE_DID
    fake.documents[ALICE_DID]["alsoKnownAs"] = [f"at://{HANDLE}"]
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser)
    assert exc.value.code == "account_mismatch"
    assert "an unverified account" in exc.value.message
    _nothing_stored(paths)


def test_a_token_from_a_server_that_does_not_speak_for_its_account_is_refused(paths, fake, browser):
    fake.sub = EVE_DID  # her PDS names another authorization server
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser)
    assert exc.value.code == "api_error" and "nothing was stored" in exc.value.message
    assert exc.value.detail == {"sub": EVE_DID, "issuer": ISSUER}
    _nothing_stored(paths)


def test_the_configured_expected_handle_is_enforced(paths, fake, browser):
    expected = AccountConfig(alias=BSKY, expected_handle="someone.bsky.social")
    settings = replace(Settings(), accounts=(expected,))
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser, settings=settings)
    assert exc.value.code == "account_mismatch"
    _nothing_stored(paths)


@pytest.mark.parametrize(
    "redirect, problem",
    [({"deny": "access_denied"}, "denied"), ({"redirect_iss": OTHER_ISSUER}, "does not come")],
)
def test_a_redirect_that_is_not_this_logins_approval_stores_nothing(
    paths, fake, browser, redirect, problem
):
    for name, value in redirect.items():
        setattr(fake, name, value)
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser)
    assert exc.value.code == "api_error" and problem in exc.value.message
    assert fake.posts("/oauth/token") == [], "no code was exchanged"
    _nothing_stored(paths)


def test_a_handle_that_resolves_nowhere_sends_no_one_to_a_browser(paths, fake, browser):
    del fake.handles[HANDLE]
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser)
    assert exc.value.code == "api_error" and "does not resolve" in exc.value.message
    assert browser.shown == [] and fake.pars == {}
    _nothing_stored(paths)


def test_a_hosted_client_metadata_document_is_the_client(paths, fake, browser):
    hosted = "https://example.org/pulsar/client-metadata.json"
    _login(paths, fake, browser, client_id=hosted)
    [par] = fake.pars.values()
    assert par["client_id"] == hosted
    assert _store(paths).load().client_id == hosted
    assert load_client_id(paths, "bsky") == hosted


@pytest.mark.parametrize(
    "client_id",
    ["http://example.org/meta.json", "https://example.org", "http://localhost:8080", "cid"],
)
def test_a_client_id_atproto_would_refuse_is_refused_first(client_id):
    with pytest.raises(PulsarError) as exc:
        check_client_id(client_id)
    assert exc.value.code == "invalid_argument"


def test_the_loopback_client_names_its_redirect_and_scope():
    client = urlsplit(BlueskyLogin().default_client_id())
    assert f"{client.scheme}://{client.netloc}" == "http://localhost" and client.path == ""
    assert parse_qs(client.query) == {
        "redirect_uri": ["http://127.0.0.1:8976/callback"],
        "scope": [SCOPE],
    }


def test_a_provider_without_a_login_is_unsupported(paths, fake, browser):
    with pytest.raises(PulsarError) as exc:
        _login(paths, fake, browser, alias="mastodon:someone")
    assert exc.value.code == "unsupported"
    assert browser.shown == []


# -- after the login --------------------------------------------------------------------


async def test_the_bound_account_posts_with_its_stored_key_and_the_pds_nonce(paths, fake, browser):
    _login(paths, fake, browser)
    async with make_runtime(paths, settings=Settings(), transport=fake.transport()) as rt:
        found = await rt.identity(BSKY, live=True)
    assert (found.handle, found.provider_user_id) == (HANDLE, DID)
    sessions = fake.pds.calls("com.atproto.server.getSession")
    assert len(sessions) == 2, "a use_dpop_nonce answer is repeated once"
    header, claims = verify_proof(sessions[1].headers["DPoP"])
    assert claims["nonce"] == PDS_NONCE
    assert claims["htu"] == f"{PDS}/xrpc/com.atproto.server.getSession"
    assert sessions[1].headers["Authorization"] == f"DPoP {ACCESS}"
    assert thumbprint(header["jwk"]) == fake.bound_jkt


async def test_refresh_waits_for_the_accounts_cross_process_lock(paths, fake, browser, monkeypatch):
    account = _login(paths, fake, browser)
    store = _store(paths)
    async with make_runtime(paths, settings=Settings(), transport=fake.transport()) as rt:
        client = rt.client_for(BSKY)
        async with store.refresh_lock(5):  # another process is refreshing this account
            blocked = blocked_on_lock(monkeypatch)
            waiting = asyncio.create_task(client.refresh(store.load()))
            assert await asyncio.to_thread(blocked.wait, 5), "the refresh waits for the lock"
            assert not waiting.done()
            refreshes = [r for r in fake.posts("/oauth/token") if b"refresh_token" in r.content]
            assert refreshes == [], "nothing is sent without the lock"
        fresh = await asyncio.wait_for(waiting, 5)
    assert (fresh.access_token, fresh.refresh_token) == (ROTATED_ACCESS, ROTATED_REFRESH)
    # The rotated pair keeps the login's key, PDS, token endpoint and binding.
    before = account.binding_id
    stored = store.load()
    assert stored == fresh and stored.binding_id == before
    assert (stored.service, stored.token_url) == (PDS, f"{ISSUER}/oauth/token")
    assert stored.token_type == "DPoP"
    refresh = [r for r in fake.posts("/oauth/token") if b"refresh_token" in r.content][-1]
    header, claims = verify_proof(refresh.headers["DPoP"])
    assert claims["nonce"] == AS_NONCE and thumbprint(header["jwk"]) == fake.bound_jkt
    assert FakeAtproto.form(refresh)["client_id"] == loopback_client_id()


async def test_an_expiring_token_is_refreshed_before_a_request(paths, fake, browser):
    _login(paths, fake, browser)
    store = _store(paths)
    store.save(replace(store.load(), expires_at=time.time() + 5), expected_previous=store.load())
    async with make_runtime(paths, settings=Settings(), transport=fake.transport()) as rt:
        await rt.identity(BSKY, live=True)
    assert store.load().access_token == ROTATED_ACCESS
    assert fake.pds.calls("com.atproto.server.getSession")[-1].headers["Authorization"] == (
        f"DPoP {ROTATED_ACCESS}"
    )


async def test_logout_forgets_the_tokens_and_the_key(paths, fake, browser):
    _login(paths, fake, browser)
    out, code = make_app(paths).logout(BSKY)
    assert code == 0 and out["tokens_removed"] is True and out["status"] == "revoked"
    assert not _store(paths).token_file.exists()
    assert AccountRegistry(paths).get(BSKY).status == "revoked"
    async with make_runtime(paths, settings=Settings(), transport=fake.transport()) as rt:
        with pytest.raises(AuthExpired):
            await rt.identity(BSKY)


async def test_a_relogin_mints_a_new_key_and_binding(paths, fake, browser):
    first = _login(paths, fake, browser)
    key = _store(paths).load().dpop_key
    second = _login(paths, fake, browser)
    stored = _store(paths).load()
    assert second.binding_id != first.binding_id and stored.dpop_key != key


async def test_auth_status_reports_bluesky_health_with_xs_fields(paths, fake, browser, bundle):
    register(paths, bundle, handle="constworks", provider_user_id="1")
    _login(paths, fake, browser)
    app = make_app(paths, transport=fake.transport())
    out, code = await app.auth_status()
    x_entry, bsky_entry = sorted(out["accounts"], key=lambda e: e["alias"] != "x:constworks")
    assert set(bsky_entry) == set(x_entry)
    assert bsky_entry["health"] == "healthy" and bsky_entry["token_state"] == "valid"
    assert bsky_entry["reauth_required"] is False and bsky_entry["mismatch"] is False
    assert bsky_entry["account"] == {"user_id": DID, "username": HANDLE}
    assert app.attention(bsky_entry) is None and code == 0
    blob = json.dumps(out)
    assert not any(s in blob for s in (*SECRETS, _store(paths).load().dpop_key))

    out, code = await app.auth_status(account=BSKY, live=True)
    [entry] = out["accounts"]
    assert entry["refreshed"] is True and entry["verified"] is True
    assert entry["account_source"] == "live" and entry["health"] == "healthy" and code == 0


async def test_a_dead_refresh_token_is_reported_as_reauth(paths, fake, browser):
    _login(paths, fake, browser)
    fake.refresh_token = "revoked-elsewhere"
    app = make_app(paths, transport=fake.transport())
    out, code = await app.auth_status(account=BSKY, live=True)
    [entry] = out["accounts"]
    assert code == 1 and entry["reauth_required"] is True and entry["health"] == "unhealthy"
    assert app.attention(entry) == (
        f"{BSKY}: re-authorization required (`PULSAR_HOME={paths.home} pulsar auth login "
        f"--account {BSKY}`)"
    )
    assert AccountRegistry(paths).get(BSKY).status == "reauth_required"


async def test_an_unusable_stored_key_sends_nothing(paths, fake, browser):
    _login(paths, fake, browser)
    store = _store(paths)
    store.save(replace(store.load(), dpop_key="not-a-key"), expected_previous=store.load())
    sent = len(fake.requests)
    async with make_runtime(paths, settings=Settings(), transport=fake.transport()) as rt:
        with pytest.raises(PulsarError) as exc:
            await rt.identity(BSKY, live=True)
    assert exc.value.code == "unsupported" and "auth login --account" in exc.value.message
    assert len(fake.requests) == sent
