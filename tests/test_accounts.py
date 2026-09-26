"""The account registry: several accounts in one home, resolved, checked and migrated."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
import time

import httpx
import pytest
from mcp.client._memory import InMemoryTransport
from mcp.client.session import ClientSession

from pulsar.core.accounts import (
    LEGACY_NEEDS_ALIAS,
    Account,
    AccountRegistry,
    canonical_alias,
    require_expected,
)
from pulsar.core.adapter import Identity
from pulsar.core.errors import PulsarError
from pulsar.core.paths import account_slug, alias_from_slug
from pulsar.core.settings import AccountConfig, Settings
from pulsar.core.store import FernetFileStore, TokenBundle
from pulsar.mcp import Runtime, build_server
from pulsar.providers.x.auth import complete_login
from pulsar.providers.x.client import XClient

from .conftest import ALIAS, REFRESH, ROTATED_ACCESS, SECRETS, register
from .lock_probe import blocked_on_lock
from .media_samples import PNG

pytestmark = pytest.mark.anyio

OTHER = "x:other"


def _codes(fn):
    with pytest.raises(PulsarError) as exc:
        fn()
    return exc.value


def _bundle(bundle, **changes):
    return TokenBundle(**{**bundle.__dict__, **changes})


def _settings(default=None, **expected):
    return Settings(
        default_account=default,
        accounts=tuple(
            AccountConfig(alias=alias, expected_handle=handle) for alias, handle in expected.items()
        ),
    )


# -- registry file and slugs ----------------------------------------------------


def test_registry_round_trip_is_private_and_holds_no_secret(paths, bundle):
    register(paths, bundle, handle="constworks", provider_user_id="1", bound_at="t0")
    registry = AccountRegistry(paths)
    row = registry.get("X:@ConstWorks")
    assert row == Account(
        alias=ALIAS,
        provider="x",
        handle="constworks",
        provider_user_id="1",
        scopes=tuple(bundle.scope.split()),
        bound_at="t0",
    )
    assert AccountRegistry(paths).accounts() == {ALIAS: row}
    assert stat.S_IMODE(os.stat(paths.accounts_file).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths.home).st_mode) == 0o700
    raw = paths.accounts_file.read_text()
    assert json.loads(raw)["accounts"][ALIAS]["status"] == "active"
    assert not any(s in raw for s in SECRETS)


def test_a_wide_registry_is_refused(paths, bundle):
    register(paths, bundle)
    os.chmod(paths.accounts_file, 0o644)
    assert _codes(AccountRegistry(paths).accounts).code == "insecure_storage"


def test_a_corrupt_registry_is_invalid_config_not_empty(paths, bundle):
    register(paths, bundle)
    paths.accounts_file.write_text("{not json")
    assert _codes(AccountRegistry(paths).accounts).code == "invalid_config"


@pytest.mark.parametrize(
    ("alias", "slug"),
    [("x:constworks", "x--constworks"), ("x:a.b-c_d", "x--a.b-c_d"), ("bsky:a--b", "bsky--a--b")],
)
def test_slugs_are_reversible(alias, slug):
    assert account_slug(alias) == slug and alias_from_slug(slug) == alias


@pytest.mark.parametrize(
    "alias",
    ["x:../../etc", "x:a/b", "x:..", "../x:foo", "x-y:foo", "x:foo bar", "x:", "x:" + "a" * 101],
)
def test_unsafe_aliases_are_refused(paths, alias):
    for attempt in (
        lambda: canonical_alias(alias),
        lambda: FernetFileStore.for_account(paths, alias),
        lambda: AccountRegistry(paths).get(alias),
    ):
        assert _codes(attempt).code == "invalid_argument"
    assert not paths.home.exists(), "nothing is created for a refused alias"


def test_alias_from_slug_ignores_foreign_names():
    assert alias_from_slug("notaslug") is None
    assert alias_from_slug(".x--foo") is None


# -- resolve ----------------------------------------------------------------------


def test_resolve_branches(paths, bundle):
    registry = AccountRegistry(paths)
    none_bound = _codes(lambda: registry.resolve(None, Settings()))
    assert none_bound.code == "auth_expired"

    register(paths, bundle)
    assert registry.resolve(None, Settings()).alias == ALIAS  # the only one
    assert registry.resolve("X:@constworks", Settings()).alias == ALIAS

    unknown = _codes(lambda: registry.resolve("x:nobody", Settings()))
    assert unknown.code == "unknown_account" and "x:constworks" in unknown.message
    assert unknown.detail == {"account": "x:nobody", "known": [ALIAS]}
    assert _codes(lambda: registry.resolve("constworks", Settings())).code == "invalid_argument"

    register(paths, _bundle(bundle, access_token="a2"), OTHER)
    several = _codes(lambda: registry.resolve(None, Settings()))
    assert several.code == "invalid_argument" and "several accounts are bound" in several.message
    assert registry.resolve(None, _settings(default=OTHER)).alias == OTHER
    missing_default = _codes(lambda: registry.resolve(None, _settings(default="x:gone")))
    assert (
        missing_default.code == "unknown_account" and "default_account" in missing_default.message
    )

    registry.mark_status(OTHER, "revoked")
    assert registry.resolve(None, Settings()).alias == ALIAS, "revoked accounts are not bound"


def test_resolve_points_at_migrate_for_unnamed_legacy_credentials(paths, bundle, legacy_store):
    legacy_store.save(bundle)
    err = _codes(lambda: AccountRegistry(paths).resolve(None, Settings()))
    assert err.code == "auth_expired" and err.message == LEGACY_NEEDS_ALIAS


# -- expected handle ----------------------------------------------------------------


def test_require_expected():
    account = Account(alias="x:cw", provider="x", handle="cw", provider_user_id="1")
    require_expected(account, Settings())
    err = _codes(lambda: require_expected(account, _settings(**{"x:cw": "constworks"})))
    assert err.code == "account_mismatch"
    assert err.detail == {"alias": "x:cw", "expected_handle": "constworks", "bound_handle": "cw"}
    impostor = Account(alias=ALIAS, provider="x", handle="impostor", provider_user_id="1")
    err = _codes(lambda: require_expected(impostor, Settings()))
    assert err.detail["expected_handle"] == "constworks"
    unverified = Account(alias=ALIAS, provider="x")
    assert _codes(lambda: require_expected(unverified, Settings())).code == "account_mismatch"


async def _call(rt, tool, args):
    async with InMemoryTransport(build_server(rt)) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            result = await s.call_tool(tool, args)
    assert not result.is_error, result
    return result.structured_content or json.loads(result.content[0].text)


async def test_a_wrong_token_under_the_right_name_cannot_post(paths, authed, fake_x):
    """2026-09-16: the stored token was another account's. Now it never posts."""
    fake_x.username = "impostor"
    rt = Runtime(paths, settings=_settings(**{ALIAS: "constworks"}), transport=fake_x.transport())
    try:
        out = await _call(rt, "create_post", {"text": "hello"})
        assert out["code"] == "account_mismatch"
        assert out["detail"] == {
            "alias": ALIAS,
            "expected_handle": "constworks",
            "bound_handle": "impostor",
        }
        png = base64.b64encode(PNG).decode()
        for tool, args in (("delete_post", {"post_id": "5"}), ("upload_media", {"base64": png})):
            assert (await _call(rt, tool, args))["code"] == "account_mismatch"
    finally:
        await rt.aclose()
    assert [r.url.path for r in fake_x.requests] == ["/2/users/me"], "only the identity check"
    assert not paths.ledger_db.exists(), "refused before the ledger claim"


# -- login ------------------------------------------------------------------------------


def test_login_refuses_a_mismatched_handle_and_stores_nothing(paths, bundle, fake_x):
    fake_x.username = "someoneelse"
    err = _codes(
        lambda: complete_login(
            paths, Settings(), ALIAS, "cid", bundle, transport=fake_x.transport()
        )
    )
    assert err.code == "account_mismatch" and "@constworks" in err.message
    assert "@someoneelse" in err.message
    assert not FernetFileStore.for_account(paths, ALIAS).token_file.exists()
    assert AccountRegistry(paths).accounts() == {} and not paths.client_file.exists()


def test_login_refuses_a_handle_other_than_the_configured_one(paths, bundle, fake_x):
    fake_x.username = "cw"
    settings = _settings(**{"x:cw": "constworks"})
    err = _codes(
        lambda: complete_login(paths, settings, "x:cw", "cid", bundle, transport=fake_x.transport())
    )
    assert err.code == "account_mismatch"
    assert err.detail == {"alias": "x:cw", "expected_handle": "constworks", "bound_handle": "cw"}
    assert AccountRegistry(paths).accounts() == {}


def test_login_rebinds_under_the_accounts_lock(paths, bundle, fake_x, monkeypatch):
    register(paths, bundle)
    store = FernetFileStore.for_account(paths, ALIAS)
    relogin = _bundle(bundle, access_token=ROTATED_ACCESS, refresh_token="r2")
    done: list[Account] = []

    def login():
        done.append(
            complete_login(paths, Settings(), ALIAS, "cid", relogin, transport=fake_x.transport())
        )

    blocked = blocked_on_lock(monkeypatch)
    with store.refresh_lock_sync(5):
        thread = threading.Thread(target=login)
        thread.start()
        assert blocked.wait(5), "login waits for a refresh in flight"
        assert thread.is_alive() and not done
    thread.join(5)
    assert done and store.load().access_token == ROTATED_ACCESS
    assert AccountRegistry(paths).get(ALIAS).binding_id == store.load().binding_id


# -- two accounts, one key -------------------------------------------------------------


async def test_two_accounts_refresh_independently(paths, bundle, monkeypatch):
    expired = time.time() - 10
    a = register(paths, _bundle(bundle, expires_at=expired, refresh_token="refresh-a"))
    b = register(paths, _bundle(bundle, expires_at=expired, refresh_token="refresh-b"), OTHER)
    assert a.token_file != b.token_file and a.lock_file != b.lock_file
    assert paths.key_file.exists() and not paths.token_file.exists()  # one key, at the root
    rotations: list[str] = []

    def token_endpoint(request):
        old = request.content.decode().split("refresh_token=")[1].split("&")[0]
        rotations.append(old)
        return httpx.Response(
            200,
            json={
                "access_token": f"access-after-{old}",
                "refresh_token": f"{old}-next",
                "expires_in": 7200,
            },
        )

    client_a = XClient(a, transport=httpx.MockTransport(token_endpoint))
    client_b = XClient(b, transport=httpx.MockTransport(token_endpoint))
    try:
        async with a.refresh_lock(5):  # account A is mid-refresh in another process
            fresh_b = await asyncio.wait_for(client_b.refresh(b.load()), 2)
            blocked = blocked_on_lock(monkeypatch)
            waiting_a = asyncio.create_task(client_a.refresh(a.load()))
            assert await asyncio.to_thread(blocked.wait, 5), "A waits for its own lock"
            assert not waiting_a.done()
        fresh_a = await asyncio.wait_for(waiting_a, 5)
    finally:
        await client_a.aclose()
        await client_b.aclose()
    assert rotations == ["refresh-b", "refresh-a"]
    assert fresh_b.refresh_token == "refresh-b-next" and b.load() == fresh_b
    assert fresh_a.refresh_token == "refresh-a-next" and a.load() == fresh_a


def _users_x(users: dict[str, tuple[str, str]], posts: list[tuple[str, str]]):
    """A fake X that knows one user per access token."""

    def handle(request):
        user = users.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if user is None:
            return httpx.Response(401, json={"title": "Unauthorized"})
        if request.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"id": user[0], "username": user[1]}})
        if request.url.path.endswith("/tweets") and request.method == "POST":
            posts.append((user[1], json.loads(request.content)["text"]))
            return httpx.Response(201, json={"data": {"id": str(100 + len(posts)), "text": "t"}})
        return httpx.Response(404)

    return httpx.MockTransport(handle)


async def test_the_account_argument_picks_who_posts(paths, bundle):
    register(paths, bundle)
    register(paths, _bundle(bundle, access_token="access-other"), OTHER)
    posts: list[tuple[str, str]] = []
    users = {bundle.access_token: ("1", "constworks"), "access-other": ("2", "other")}
    rt = Runtime(paths, settings=Settings(), transport=_users_x(users, posts))
    try:
        several = await _call(rt, "create_post", {"text": "who am i"})
        assert several["code"] == "invalid_argument"
        out = await _call(rt, "create_post", {"text": "hi", "account": "X:@Other"})
        assert out["url"] == "https://x.com/other/status/101"
        assert (await _call(rt, "whoami", {"account": ALIAS}))["username"] == "constworks"
        unknown = await _call(rt, "whoami", {"account": "x:nobody"})
        assert unknown["code"] == "unknown_account"
        traversal = await _call(rt, "whoami", {"account": "x:../../etc"})
        assert traversal["code"] == "invalid_argument"
    finally:
        await rt.aclose()
    assert posts == [("other", "hi")]
    row = rt.ledger.get(json.loads(paths.write_log.read_text())["idempotency_key"])
    assert (row.account_user_id, row.account_handle) == ("2", "other")


async def test_a_failed_refresh_marks_the_account_reauth_required(paths, authed, fake_x):
    fake_x.fail_auth_once = True
    fake_x.refresh_status = 401
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())
    try:
        assert (await _call(rt, "create_post", {"text": "hello"}))["code"] == "auth_expired"
    finally:
        await rt.aclose()
    assert AccountRegistry(paths).get(ALIAS).status == "reauth_required"


# -- logout -----------------------------------------------------------------------------


async def test_logout_revokes_under_the_lock_and_keeps_history(paths, bundle, fake_x, monkeypatch):
    store = register(paths, bundle, handle="constworks", provider_user_id="1")
    registry = AccountRegistry(paths)
    blocked = blocked_on_lock(monkeypatch)
    with store.refresh_lock_sync(5):
        thread = threading.Thread(target=registry.logout, args=(ALIAS,))
        thread.start()
        assert blocked.wait(5), "logout waits for a refresh in flight"
        assert thread.is_alive()
        assert registry.get(ALIAS).status == "active"
    thread.join(5)
    row = registry.get(ALIAS)
    assert store.load() is None and row.status == "revoked" and row.handle == "constworks"
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())
    try:
        out = await _call(rt, "whoami", {"account": ALIAS})
    finally:
        await rt.aclose()
    assert out["code"] == "auth_expired" and "logged out" in out["message"]
    assert _codes(lambda: registry.logout("x:nobody")).code == "unknown_account"


# -- identity cache ------------------------------------------------------------------------


def test_identity_is_trusted_only_for_the_binding_it_describes(paths, bundle):
    store = register(paths, bundle)
    stored = store.rebind(bundle)
    registry = AccountRegistry(paths)
    registry.mark_verified(ALIAS, Identity("1", "constworks"), stored.binding_id)
    row = registry.get(ALIAS)
    assert registry.trusted_identity(row, store.load()) == Identity("1", "constworks")
    registry.mark_verified(ALIAS, Identity("9", "old"), "a-binding-that-raced")
    assert registry.trusted_identity(registry.get(ALIAS), store.load()) is None


# -- migration from the single-account layout --------------------------------------------


def _legacy(paths, legacy_store, bundle, *, whoami: str | None = "constworks", binding=True):
    stored = legacy_store.rebind(bundle)
    if whoami is not None:
        record = {"user_id": "1234567890", "username": whoami}
        record["binding_id"] = stored.binding_id if binding else "some-older-login"
        paths.whoami_cache.write_text(json.dumps(record))
    return stored


def _migrated(paths, stored, *, handle="constworks"):
    """The converged state after migrating ``stored`` as x:constworks."""
    registry = AccountRegistry(paths)
    row = registry.get(ALIAS)
    assert FernetFileStore.for_account(paths, ALIAS).load() == stored
    assert not paths.token_file.exists() and not paths.whoami_cache.exists()
    assert row.binding_id == stored.binding_id and row.status == "active"
    assert row.handle == handle
    assert list(registry.accounts()) == [ALIAS]


async def test_first_use_migrates_via_a_matching_whoami(paths, bundle, legacy_store, fake_x):
    stored = _legacy(paths, legacy_store, bundle)
    rt = Runtime(paths, settings=Settings(), transport=fake_x.transport())
    try:
        assert (await rt.whoami())["username"] == "constworks"
    finally:
        await rt.aclose()
    _migrated(paths, stored)
    assert fake_x.requests == [], "the migrated identity is trusted: same binding"


async def test_first_use_migrates_via_default_account(paths, bundle, legacy_store, fake_x):
    stored = _legacy(paths, legacy_store, bundle, whoami=None)
    result = AccountRegistry(paths).migrate_legacy(_settings(default=ALIAS))
    assert (result.state, result.alias) == ("migrated", ALIAS)
    _migrated(paths, stored, handle=None)
    rt = Runtime(paths, settings=_settings(default=ALIAS), transport=fake_x.transport())
    try:
        assert (await _call(rt, "create_post", {"text": "hi"}))["ok"] is True
    finally:
        await rt.aclose()
    assert fake_x.calls("GET", "/users/me"), "an unverified migrated account is checked live"


@pytest.mark.parametrize("whoami", [None, "stale-binding"])
def test_legacy_without_a_name_stays_put(paths, bundle, legacy_store, whoami):
    _legacy(paths, legacy_store, bundle, whoami=whoami and "constworks", binding=whoami is None)
    registry = AccountRegistry(paths)
    result = registry.migrate_legacy(Settings())
    assert result.state == "needs_alias" and result.message == LEGACY_NEEDS_ALIAS
    assert paths.token_file.exists() and registry.accounts() == {}
    assert registry.migrate_legacy(Settings(), "x:constworks").state == "migrated"


def test_legacy_for_another_handle_is_a_mismatch_and_moves_nothing(paths, bundle, legacy_store):
    _legacy(paths, legacy_store, bundle, whoami="someoneelse")
    err = _codes(lambda: AccountRegistry(paths).migrate_legacy(_settings(default=ALIAS)))
    assert err.code == "account_mismatch"
    assert paths.token_file.exists() and AccountRegistry(paths).accounts() == {}


def test_legacy_is_left_alone_once_accounts_exist(paths, bundle, legacy_store):
    _legacy(paths, legacy_store, bundle)
    register(paths, _bundle(bundle, access_token="a2"), OTHER)
    result = AccountRegistry(paths).migrate_legacy(Settings())
    assert result.state == "ignored" and "pulsar auth migrate" in (result.message or "")
    assert paths.token_file.exists()


def test_migrate_never_overwrites_an_accounts_credentials(paths, bundle, legacy_store):
    _legacy(paths, legacy_store, bundle, whoami=None)
    current = _bundle(bundle, access_token="current")
    register(paths, current)
    err = _codes(lambda: AccountRegistry(paths).migrate_legacy(Settings(), ALIAS))
    assert err.code == "invalid_argument" and "already has credentials" in err.message
    assert FernetFileStore.for_account(paths, ALIAS).load() == current
    assert paths.token_file.exists()


def test_migrate_refuses_a_non_x_alias(paths, bundle, legacy_store):
    _legacy(paths, legacy_store, bundle, whoami=None)
    err = _codes(lambda: AccountRegistry(paths).migrate_legacy(Settings(), "bsky:constworks"))
    assert err.code == "invalid_argument" and paths.token_file.exists()


class Crash(Exception):
    pass


def _crash_once(monkeypatch, target, name):
    real = getattr(target, name)
    calls = {"n": 0}

    def crash(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Crash(name)
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, crash)


@pytest.mark.parametrize(
    "crash_point",
    [
        "before_rename",  # the account dir exists, the bundle is still at the root
        "after_rename",  # the bundle moved, no registry row yet
        "after_registry",  # the row is written, whoami.json not yet removed
    ],
)
def test_migration_converges_after_a_crash_at_any_step(
    paths, bundle, legacy_store, monkeypatch, crash_point
):
    stored = _legacy(paths, legacy_store, bundle)
    if crash_point == "before_rename":
        _crash_once(monkeypatch, os, "replace")
    elif crash_point == "after_rename":
        _crash_once(monkeypatch, AccountRegistry, "_write")
    else:
        _crash_once(monkeypatch, AccountRegistry, "_drop_legacy_identity")
    with pytest.raises(Crash):
        AccountRegistry(paths).migrate_legacy(Settings())
    monkeypatch.undo()
    # The only refresh token survived the crash, at the root or in the account dir.
    at_root = legacy_store.load() if paths.token_file.exists() else None
    moved = FernetFileStore.for_account(paths, ALIAS)
    assert stored in (at_root, moved.load() if moved.token_file.exists() else None)

    result = AccountRegistry(paths).migrate_legacy(Settings())  # the next process
    assert result.state in ("migrated", "none")
    if crash_point == "after_rename":
        assert result.adopted == (ALIAS,)
    _migrated(paths, stored)
    assert AccountRegistry(paths).migrate_legacy(Settings()).state == "none"


def test_migration_holds_the_legacy_refresh_lock(paths, bundle, legacy_store, monkeypatch):
    _legacy(paths, legacy_store, bundle)
    done = threading.Event()

    def migrate():
        AccountRegistry(paths).migrate_legacy(Settings())
        done.set()

    blocked = blocked_on_lock(monkeypatch)
    with legacy_store.refresh_lock_sync(5):  # a phase 1 process mid-refresh
        thread = threading.Thread(target=migrate)
        thread.start()
        assert blocked.wait(5)
        assert not done.is_set() and paths.token_file.exists()
    thread.join(5)
    assert done.is_set() and not paths.token_file.exists()


def test_migrated_bundle_refreshes_as_the_account(paths, bundle, legacy_store, token_endpoint):
    _legacy(paths, legacy_store, _bundle(bundle, expires_at=time.time() - 10))
    AccountRegistry(paths).migrate_legacy(Settings())
    store = FernetFileStore.for_account(paths, ALIAS)
    assert store.load().refresh_token == REFRESH

    async def refresh():
        client = XClient(store, transport=token_endpoint.transport())
        try:
            return await client.refresh(store.load())
        finally:
            await client.aclose()

    fresh = asyncio.run(refresh())
    assert store.load() == fresh
    assert fresh.binding_id == AccountRegistry(paths).get(ALIAS).binding_id, "binding carried"


# -- non-mutating legacy status -----------------------------------------------------------


def _snapshot(root):
    """Every path under ``root`` with its bytes (None for directories) and mtime."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        out[str(p)] = (None if p.is_dir() else p.read_bytes(), st.st_mtime_ns, st.st_mode)
    return out


@pytest.mark.parametrize(
    ("setup", "settings", "state", "alias"),
    [
        ("whoami", Settings(), "pending", ALIAS),
        ("no-name", Settings(), "needs_alias", None),
        ("no-name", "default", "pending", ALIAS),
        ("beside-accounts", Settings(), "ignored", None),
        ("orphan", Settings(), "pending", None),
        ("nothing", Settings(), "none", None),
    ],
)
def test_legacy_status_reports_without_changing_anything(
    paths, bundle, legacy_store, setup, settings, state, alias
):
    if settings == "default":
        settings = _settings(default=ALIAS)
    if setup in ("whoami", "beside-accounts"):
        _legacy(paths, legacy_store, bundle)
    elif setup == "no-name":
        _legacy(paths, legacy_store, bundle, whoami=None)
    if setup == "beside-accounts":
        register(paths, _bundle(bundle, access_token="a2"), OTHER)
    if setup == "orphan":
        FernetFileStore.for_account(paths, ALIAS).save(bundle)  # moved, no row yet
    if setup == "nothing":
        register(paths, bundle)
    before = _snapshot(paths.home)
    result = AccountRegistry(paths).legacy_status(settings)
    assert (result.state, result.alias) == (state, alias)
    if setup == "orphan":
        assert result.adopted == (ALIAS,)
    assert _snapshot(paths.home) == before, "legacy_status must not touch the home"
    if state in ("pending", "needs_alias", "ignored"):
        assert result.message


def test_legacy_status_predicts_what_migrate_does(paths, bundle, legacy_store):
    _legacy(paths, legacy_store, bundle)
    registry = AccountRegistry(paths)
    predicted = registry.legacy_status(Settings())
    done = registry.migrate_legacy(Settings())
    assert (predicted.state, predicted.alias) == ("pending", done.alias)
    assert done.state == "migrated"
    assert registry.legacy_status(Settings()).state == "none"


def test_legacy_status_of_a_missing_home_creates_nothing(paths):
    assert AccountRegistry(paths).legacy_status(Settings()).state == "none"
    assert not paths.home.exists()


# -- bounded accounts lock, newer registries --------------------------------------------


def test_the_accounts_lock_times_out_naming_its_holder(paths, bundle, monkeypatch):
    from pulsar.core.fsutil import hold_lock

    register(paths, bundle)
    monkeypatch.setattr("pulsar.core.accounts.ACCOUNTS_LOCK_WAIT_SECONDS", 0.2)
    held, release = threading.Event(), threading.Event()

    def holder():
        with hold_lock(paths.accounts_lock, label="a stuck login", what="t", timeout=5):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    try:
        err = _codes(lambda: AccountRegistry(paths).mark_status(ALIAS, "revoked"))
    finally:
        release.set()
        thread.join(5)
    assert err.code == "lock_timeout" and err.retryable is True
    assert err.detail["holder"]["label"] == "a stuck login"
    assert "a stuck login" in err.message and str(paths.accounts_lock) in err.message
    assert AccountRegistry(paths).get(ALIAS).status == "active"


def _newer_registry(paths, **declared):
    doc = json.loads(paths.accounts_file.read_text())
    doc["version"] = 2
    doc["min_reader_version"] = 1  # version 2 only added fields
    doc.update(declared)
    doc["accounts"][ALIAS]["posting_window"] = "weekdays"  # a field this pulsar does not know
    paths.accounts_file.write_text(json.dumps(doc))
    return paths.accounts_file.read_bytes()


@pytest.mark.parametrize("min_reader", [2, None, "1", True])
def test_a_newer_registry_that_does_not_declare_this_reader_is_refused(paths, bundle, min_reader):
    """Newer state is read only when its writer declared it compatible."""
    register(paths, bundle, handle="constworks", provider_user_id="1")
    before = _newer_registry(paths, min_reader_version=min_reader)
    err = _codes(lambda: AccountRegistry(paths).resolve(None, Settings()))
    assert err.code == "invalid_config", "an undeclared newer registry is not reinterpreted"
    assert "does not declare version 1 a reader" in err.message
    assert err.detail == {"path": str(paths.accounts_file), "version": 2, "supported": 1}
    assert paths.accounts_file.read_bytes() == before


def test_the_registry_declares_its_oldest_reader(paths, bundle):
    register(paths, bundle)
    doc = json.loads(paths.accounts_file.read_text())
    assert doc["version"] == 1 and doc["min_reader_version"] == 1


def test_a_newer_registry_is_read_but_never_rewritten(paths, bundle, fake_x):
    register(paths, bundle, handle="constworks", provider_user_id="1")
    before = _newer_registry(paths)
    registry = AccountRegistry(paths)
    assert registry.resolve(None, Settings()).alias == ALIAS, "reading is fine"
    for write in (
        lambda: registry.mark_status(ALIAS, "revoked"),
        lambda: registry.put(Account(alias=OTHER, provider="x")),
        lambda: registry.logout(ALIAS),
        lambda: complete_login(
            paths,
            Settings(),
            ALIAS,
            "cid",
            _bundle(bundle, access_token=ROTATED_ACCESS),
            transport=fake_x.transport(),
        ),
    ):
        err = _codes(write)
        assert err.code == "invalid_config"
        assert str(paths.accounts_file) in err.message and "version 2" in err.message
        assert err.detail == {"path": str(paths.accounts_file), "version": 2, "supported": 1}
    assert paths.accounts_file.read_bytes() == before
    assert FernetFileStore.for_account(paths, ALIAS).load() == bundle, "login stored nothing"


def test_a_registry_error_names_the_resolved_file(paths, bundle):
    register(paths, bundle)
    paths.accounts_file.write_text('{"version": "one", "accounts": {}}')
    err = _codes(AccountRegistry(paths).accounts)
    assert err.code == "invalid_config" and err.message.startswith(f"{paths.accounts_file}: ")


def test_an_unknown_alias_names_the_home_in_its_remedy(paths, bundle):
    register(paths, bundle)
    err = _codes(lambda: AccountRegistry(paths).resolve("x:nobody", Settings()))
    assert f"PULSAR_HOME={paths.home} pulsar auth login --account x:nobody" in err.message
