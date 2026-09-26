import json
import os
import stat
import threading

import pytest
from cryptography.fernet import Fernet

from pulsar.auth import bind
from pulsar.errors import PulsarError
from pulsar.server import Runtime
from pulsar.store import CredentialConflict, FernetFileStore, TokenBundle, TokenStore
from pulsar.writelog import WriteLog


def test_round_trip_and_private_modes(store, bundle, paths):
    assert store.load() is None
    store.save(bundle)
    assert store.load() == bundle
    for p in (paths.key_file, paths.token_file):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(paths.home).st_mode) == 0o700


def test_token_file_is_not_plaintext(store, bundle, paths):
    store.save(bundle)
    raw = paths.token_file.read_bytes()
    assert bundle.access_token.encode() not in raw
    assert bundle.refresh_token.encode() not in raw


def test_clear_removes_tokens_and_cache(store, bundle, paths):
    store.save(bundle)
    paths.whoami_cache.write_text(json.dumps({"user_id": "1", "username": "x"}))
    store.clear()
    assert store.load() is None
    assert not paths.whoami_cache.exists()
    assert paths.key_file.exists()  # key survives; a new bundle reuses it


def test_wrong_key_yields_none(store, bundle, paths):
    store.save(bundle)
    from cryptography.fernet import Fernet

    paths.key_file.write_bytes(Fernet.generate_key())
    assert store.load() is None


def test_from_token_response_defaults():
    b = TokenBundle.from_token_response(
        {"access_token": "a", "expires_in": 10}, client_id="c", now=1000.0
    )
    assert b.expires_at == 1010.0
    assert b.refresh_token is None
    assert b.token_type == "bearer"


def test_rebinding_drops_the_cached_identity(store, bundle, paths):
    from pulsar.auth import bind, load_client_id

    store.save(bundle)
    paths.whoami_cache.write_text('{"user_id": "1", "username": "old-account"}\n')
    bind(paths, "client-new", bundle)
    assert not paths.whoami_cache.exists(), "stale whoami must not survive a re-login"
    assert load_client_id(paths) == "client-new"
    assert store.load() == bundle


def test_token_store_is_the_fernet_file_store():
    assert TokenStore is FernetFileStore


# -- atomic save and key creation ---------------------------------------------


def test_crash_mid_save_keeps_the_previous_bundle(store, bundle, paths, monkeypatch):
    store.save(bundle)
    rotated = TokenBundle(**{**bundle.__dict__, "refresh_token": "refresh-next-EEEE"})

    def crash(*_args):
        raise OSError("power cut")

    monkeypatch.setattr("pulsar.fsutil.os.replace", crash)
    with pytest.raises(OSError):
        store.save(rotated)
    monkeypatch.undo()
    assert store.load() == bundle, "the only refresh token must survive a failed save"
    assert sorted(p.name for p in paths.home.iterdir()) == ["key", "tokens.enc"]


def test_losing_the_key_creation_race_adopts_the_winners_key(store, bundle, paths, monkeypatch):
    paths.ensure()
    winner = Fernet.generate_key()
    real_link = os.link

    def another_process_wins(src, dst):
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, winner)
        os.close(fd)
        real_link(src, dst)  # now FileExistsError, as for the real loser

    monkeypatch.setattr("pulsar.store.os.link", another_process_wins)
    store.save(bundle)
    assert paths.key_file.read_bytes() == winner
    assert Fernet(winner).decrypt(paths.token_file.read_bytes())
    assert [p.name for p in paths.home.iterdir() if p.name.startswith(".key.")] == []


def test_concurrent_first_saves_share_one_key(paths, bundle):
    barrier = threading.Barrier(8)
    keys: list[bytes] = []

    def first_save():
        barrier.wait()
        TokenStore(paths).save(bundle)
        keys.append(paths.key_file.read_bytes())

    threads = [threading.Thread(target=first_save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1
    assert TokenStore(paths).load() == bundle


def test_cas_save_refuses_a_bundle_it_did_not_expect(store, bundle, paths):
    store.save(bundle)
    other = TokenBundle(**{**bundle.__dict__, "access_token": "access-other-FFFF"})
    with pytest.raises(CredentialConflict):
        store.save(other, expected_previous=other)
    assert store.load() == bundle
    store.save(other, expected_previous=bundle)
    assert store.load() == other


# -- insecure storage -----------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "mode", "fix"),
    [
        ("home", 0o755, "chmod 700"),
        ("home", 0o710, "chmod 700"),
        ("key_file", 0o644, "chmod 600"),
        ("token_file", 0o640, "chmod 600"),
        ("token_file", 0o602, "chmod 600"),
    ],
)
def test_wide_modes_are_refused_not_treated_as_logged_out(store, authed, paths, target, mode, fix):
    path = getattr(paths, target)
    os.chmod(path, mode)
    for attempt in (store.load, lambda: store.save(authed)):
        with pytest.raises(PulsarError) as exc:
            attempt()
        assert exc.value.code == "insecure_storage"
        assert str(path) in exc.value.message
        assert f"{mode:04o}" in exc.value.message
        assert exc.value.detail["fix"] == f"{fix} {path}"
    assert stat.S_IMODE(os.stat(path).st_mode) == mode, "refuse, never silently fix"


def test_foreign_owner_is_refused(store, authed, paths, monkeypatch):
    monkeypatch.setattr("pulsar.store.os.geteuid", lambda: os.getuid() + 1)
    with pytest.raises(PulsarError) as exc:
        store.load()
    assert exc.value.code == "insecure_storage"
    assert "owned by uid" in exc.value.message


def test_missing_home_is_just_not_authorized(store):
    assert store.load() is None


@pytest.mark.anyio
async def test_every_file_pulsar_creates_is_0600_under_a_loose_umask(
    private_umask, paths, bundle, fake_x
):
    bind(paths, "client-xyz", bundle)  # key, tokens.enc, client.json
    rt = Runtime(paths, transport=fake_x.transport())
    try:
        await rt.whoami()  # whoami.json
    finally:
        await rt.client.aclose()
    rt.log.append(tool="create_post", caller="t", text="hi")  # writes.jsonl
    WriteLog(paths).append(tool="delete_post", caller="t", post_id="1")
    async with rt.store.refresh_lock(1.0):  # refresh.lock
        pass
    names = {"key", "tokens.enc", "client.json", "whoami.json", "writes.jsonl", "refresh.lock"}
    assert {p.name for p in paths.home.iterdir()} == names
    for p in paths.home.iterdir():
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600, p.name
    assert stat.S_IMODE(os.stat(paths.home).st_mode) == 0o700


def test_corrupt_key_is_a_structured_error_not_a_traceback(store, authed, paths):
    paths.key_file.write_bytes(b"not-a-key")
    with pytest.raises(PulsarError) as exc:
        store.load()
    assert exc.value.code == "insecure_storage"
    assert "not-a-key" not in exc.value.message


def test_ensure_refuses_rather_than_silently_narrowing_a_wide_home(paths):
    paths.home.mkdir(mode=0o755)
    os.chmod(paths.home, 0o755)
    with pytest.raises(PulsarError) as exc:
        paths.ensure()
    assert exc.value.code == "insecure_storage"
    assert stat.S_IMODE(os.stat(paths.home).st_mode) == 0o755, "never fixed behind the operator"


@pytest.mark.anyio
async def test_login_waits_for_a_refresh_in_flight_and_wins(store, bundle, paths):
    """A refresher that passed its compare must not write the old account over a new login."""
    import asyncio
    import dataclasses

    store.save(bundle)
    relogin = dataclasses.replace(bundle, access_token="access-relogin", refresh_token="r2")
    rotated = dataclasses.replace(bundle, access_token="access-rotated", refresh_token="r3")
    async with store.refresh_lock(5):
        assert store.load() == bundle  # the refresher's compare passes
        login = threading.Thread(target=bind, args=(paths, "client-xyz", relogin))
        login.start()
        await asyncio.sleep(0.2)
        assert login.is_alive(), "login must wait for the refresh lock"
        store.save(rotated, expected_previous=bundle)
    login.join(5)
    assert store.load().access_token == "access-relogin"


@pytest.mark.anyio
async def test_logout_waits_for_a_refresh_in_flight(store, bundle, paths):
    import asyncio

    store.save(bundle)
    async with store.refresh_lock(5):
        logout = threading.Thread(target=store.clear)
        logout.start()
        await asyncio.sleep(0.2)
        assert logout.is_alive()
        store.save(bundle, expected_previous=bundle)
    logout.join(5)
    assert store.load() is None


def test_login_gives_up_boundedly_when_the_lock_is_stuck(store, bundle, paths, monkeypatch):
    import fcntl

    monkeypatch.setattr("pulsar.store.REFRESH_LOCK_WAIT_SECONDS", 0.2)
    store.save(bundle)
    fd = os.open(paths.refresh_lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(PulsarError) as exc:
            bind(paths, "client-xyz", bundle)
        assert exc.value.code == "api_error"
    finally:
        os.close(fd)


def test_refresh_keeps_the_binding_id(store, bundle):
    stored = store.rebind(bundle)
    assert stored.binding_id and store.load().binding_id == stored.binding_id


def test_undecodable_bundle_is_auth_expired(store, authed, paths):
    from cryptography.fernet import Fernet as F

    key = paths.key_file.read_bytes().strip()
    from pulsar.fsutil import write_private_atomic

    write_private_atomic(paths.token_file, F(key).encrypt(b'{"unexpected": 1}'))
    with pytest.raises(PulsarError) as exc:
        store.load()
    assert exc.value.code == "auth_expired"
