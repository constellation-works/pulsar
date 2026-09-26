import asyncio
import dataclasses
import json
import os
import stat
import threading

import pytest
from cryptography.fernet import Fernet

from pulsar.accounts import CredentialConflict, FernetFileStore, TokenBundle
from pulsar.app import WriteLog
from pulsar.channels.x import load_client_id, save_client_id
from pulsar.errors import PulsarError
from pulsar.home import Paths, hold_lock, write_private_atomic
from pulsar.home import files as fsutil

from .conftest import make_runtime, register
from .lock_probe import blocked_on_lock


def test_round_trip_and_private_modes(store, bundle, paths):
    assert store.load() is None
    store.save(bundle)
    assert store.load() == bundle
    assert store.token_file == paths.home / "accounts" / "x--constworks" / "tokens.enc"
    for p in (paths.key_file, store.token_file):
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    for d in (paths.home, paths.accounts_dir, store.account_dir):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_token_file_is_not_plaintext(store, bundle, paths):
    store.save(bundle)
    raw = store.token_file.read_bytes()
    assert bundle.access_token.encode() not in raw
    assert bundle.refresh_token.encode() not in raw


def test_clear_removes_tokens_and_cache(legacy_store, bundle, paths):
    legacy_store.save(bundle)
    paths.whoami_cache.write_text(json.dumps({"user_id": "1", "username": "x"}))
    legacy_store.clear()
    assert legacy_store.load() is None
    assert not paths.whoami_cache.exists()
    assert paths.key_file.exists()  # key survives; a new bundle reuses it


def test_account_clear_keeps_the_key_and_other_accounts(store, bundle, paths):
    store.save(bundle)
    other = FernetFileStore.for_account(paths, "x:other")
    other.save(bundle)
    store.clear()
    assert store.load() is None and other.load() == bundle
    assert paths.key_file.exists()


def test_a_replaced_key_is_unreadable_not_logged_out(store, bundle, paths):
    store.save(bundle)
    before = store.token_file.read_bytes()
    paths.key_file.write_bytes(Fernet.generate_key())
    with pytest.raises(PulsarError) as exc:
        store.load()
    err = exc.value
    assert err.code == "credentials_unreadable" and err.retryable is False
    assert str(store.token_file) in err.message and str(paths.key_file) in err.message
    assert "restore the key" in err.message
    assert f"PULSAR_HOME={paths.home} pulsar auth login --account x:constworks" in err.message
    assert err.detail == {"path": str(store.token_file), "key": str(paths.key_file)}
    assert store.token_file.read_bytes() == before, "never overwritten"


def test_from_token_response_defaults():
    b = TokenBundle.from_token_response(
        {"access_token": "a", "expires_in": 10}, client_id="c", now=1000.0
    )
    assert b.expires_at == 1010.0
    assert b.refresh_token is None
    assert b.token_type == "bearer"


def test_a_token_response_without_expires_in_is_already_expiring():
    """No invented lifetime: the next call refreshes first."""
    b = TokenBundle.from_token_response({"access_token": "a"}, client_id="c", now=1000.0)
    assert b.expires_at == 1000.0


def test_rebinding_drops_the_cached_identity(legacy_store, bundle, paths):
    legacy_store.save(bundle)
    paths.whoami_cache.write_text('{"user_id": "1", "username": "old-account"}\n')
    legacy_store.rebind(bundle)
    assert not paths.whoami_cache.exists(), "stale whoami must not survive a re-login"
    assert legacy_store.load() == bundle


def test_client_id_is_saved_under_the_accounts_lock(paths, monkeypatch):
    save_client_id(paths, "client-a")
    blocked = blocked_on_lock(monkeypatch)
    thread, release = _hold_in_thread(paths.accounts_lock, "another login")
    saver = threading.Thread(target=save_client_id, args=(paths, "client-b"))
    saver.start()
    assert blocked.wait(5), "the save waits for the accounts lock"
    assert load_client_id(paths) == "client-a"
    release.set()
    thread.join(5)
    saver.join(5)
    assert load_client_id(paths) == "client-b"


def test_a_group_writable_or_symlinked_client_json_is_refused(paths, tmp_path):
    save_client_id(paths, "client-a")
    os.chmod(paths.client_file, 0o664)
    with pytest.raises(PulsarError) as exc:
        load_client_id(paths)
    assert exc.value.code == "insecure_storage"
    os.chmod(paths.client_file, 0o600)
    real = tmp_path / "client.json"
    os.replace(paths.client_file, real)
    paths.client_file.symlink_to(real)
    with pytest.raises(PulsarError) as exc:
        load_client_id(paths)
    assert "is a symlink" in exc.value.message


def test_client_id_is_one_per_provider_and_reads_the_legacy_format(paths):
    paths.ensure()
    paths.client_file.write_text('{"client_id": "legacy-id", "redirect_uri": "x"}\n')
    os.chmod(paths.client_file, 0o600)  # as phase 1 wrote it
    assert load_client_id(paths) == "legacy-id"
    save_client_id(paths, "client-new")
    assert load_client_id(paths) == "client-new"
    assert json.loads(paths.client_file.read_text())["x"]["client_id"] == "client-new"
    assert stat.S_IMODE(os.stat(paths.client_file).st_mode) == 0o600


# -- atomic save and key creation ---------------------------------------------


def test_crash_mid_save_keeps_the_previous_bundle(store, bundle, paths, monkeypatch):
    store.save(bundle)
    rotated = TokenBundle(**{**bundle.__dict__, "refresh_token": "refresh-next-EEEE"})

    def crash(*_args):
        raise OSError("power cut")

    monkeypatch.setattr("pulsar.home.files.os.replace", crash)
    with pytest.raises(OSError):
        store.save(rotated)
    monkeypatch.undo()
    assert store.load() == bundle, "the only refresh token must survive a failed save"
    assert sorted(p.name for p in paths.home.iterdir()) == ["accounts", "key"]
    assert sorted(p.name for p in store.account_dir.iterdir()) == ["tokens.enc"]


def test_losing_the_key_creation_race_adopts_the_winners_key(store, bundle, paths, monkeypatch):
    paths.ensure()
    winner = Fernet.generate_key()
    real_link = os.link

    def another_process_wins(src, dst):
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, winner)
        os.close(fd)
        real_link(src, dst)  # now FileExistsError, as for the real loser

    monkeypatch.setattr("pulsar.home.files.os.link", another_process_wins)
    store.save(bundle)
    assert paths.key_file.read_bytes() == winner
    assert Fernet(winner).decrypt(store.token_file.read_bytes())
    assert [p.name for p in paths.home.iterdir() if p.name.startswith(".key.")] == []


def test_concurrent_first_saves_share_one_key(paths, bundle):
    barrier = threading.Barrier(8)
    keys: list[bytes] = []

    def first_save():
        barrier.wait()
        FernetFileStore.for_account(paths, "x:constworks").save(bundle)
        keys.append(paths.key_file.read_bytes())

    threads = [threading.Thread(target=first_save) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(keys)) == 1
    assert FernetFileStore.for_account(paths, "x:constworks").load() == bundle


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
        ("accounts_dir", 0o755, "chmod 700"),
        ("account_dir", 0o750, "chmod 700"),
        ("key_file", 0o644, "chmod 600"),
        ("token_file", 0o640, "chmod 600"),
        ("token_file", 0o602, "chmod 600"),
    ],
)
def test_wide_modes_are_refused_not_treated_as_logged_out(store, authed, paths, target, mode, fix):
    path = getattr(store, target, None) or getattr(paths, target)
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
    monkeypatch.setattr("pulsar.home.files.os.geteuid", lambda: os.getuid() + 1)
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
    register(paths, bundle)  # key, accounts/x--constworks/tokens.enc, accounts.json/.lock
    save_client_id(paths, "client-xyz")  # client.json
    rt = make_runtime(paths, transport=fake_x.transport())
    try:
        await rt.whoami()  # the identity lands in accounts.json
    finally:
        await rt.aclose()
    rt.log.append(tool="create_post", caller="t", text="hi")  # writes.jsonl
    WriteLog(paths).append(tool="delete_post", caller="t", post_id="1")
    async with rt.store.refresh_lock(1.0):  # accounts/x--constworks/refresh.lock
        pass
    names = {"key", "client.json", "accounts.json", "accounts.lock", "writes.jsonl", "accounts"}
    assert {p.name for p in paths.home.iterdir()} == names
    account_dir = rt.store.account_dir
    assert {p.name for p in account_dir.iterdir()} == {"tokens.enc", "refresh.lock"}
    for p in [*paths.home.iterdir(), *account_dir.iterdir()]:
        want = 0o700 if p.is_dir() else 0o600
        assert stat.S_IMODE(os.stat(p).st_mode) == want, p.name
    for d in (paths.home, account_dir):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


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
async def test_login_waits_for_a_refresh_in_flight_and_wins(store, bundle, paths, monkeypatch):
    """A refresher that passed its compare must not write the old account over a new login."""
    store.save(bundle)
    relogin = dataclasses.replace(bundle, access_token="access-relogin", refresh_token="r2")
    rotated = dataclasses.replace(bundle, access_token="access-rotated", refresh_token="r3")
    blocked = blocked_on_lock(monkeypatch)
    async with store.refresh_lock(5):
        assert store.load() == bundle  # the refresher's compare passes
        login = threading.Thread(target=store.rebind, args=(relogin,))
        login.start()
        assert await asyncio.to_thread(blocked.wait, 5), "login must wait for the refresh lock"
        assert login.is_alive()
        store.save(rotated, expected_previous=bundle)
    login.join(5)
    assert store.load().access_token == "access-relogin"


@pytest.mark.anyio
async def test_logout_waits_for_a_refresh_in_flight(store, bundle, paths, monkeypatch):
    store.save(bundle)
    blocked = blocked_on_lock(monkeypatch)
    async with store.refresh_lock(5):
        logout = threading.Thread(target=store.clear)
        logout.start()
        assert await asyncio.to_thread(blocked.wait, 5)
        assert logout.is_alive()
        store.save(bundle, expected_previous=bundle)
    logout.join(5)
    assert store.load() is None


def _hold_in_thread(path, label):
    """Hold ``path``'s lock from another thread until the returned event is set."""
    held, release = threading.Event(), threading.Event()

    def holder():
        with hold_lock(path, label=label, what="test", timeout=5):
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    return thread, release


def test_login_gives_up_boundedly_and_names_the_holder(store, bundle, paths, monkeypatch):
    monkeypatch.setattr("pulsar.accounts.store.REFRESH_LOCK_WAIT_SECONDS", 0.2)
    store.save(bundle)
    thread, release = _hold_in_thread(store.lock_file, "token refresh of x:constworks")
    try:
        with pytest.raises(PulsarError) as exc:
            store.rebind(bundle)
    finally:
        release.set()
        thread.join(5)
    err = exc.value
    assert err.code == "lock_timeout" and err.retryable is True
    holder = err.detail["holder"]
    assert holder["pid"] == os.getpid() and holder["label"] == "token refresh of x:constworks"
    assert holder["acquired_at"]
    assert f"pid {os.getpid()} (token refresh of x:constworks" in err.message
    assert str(store.lock_file) in err.message and err.detail["lock"] == str(store.lock_file)


def test_a_lock_timeout_without_a_holder_record_says_unknown(store, bundle, monkeypatch):
    import fcntl

    monkeypatch.setattr("pulsar.accounts.store.REFRESH_LOCK_WAIT_SECONDS", 0.2)
    store.save(bundle)
    fd = os.open(store.lock_file, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # an older pulsar: holds it, records nothing
        with pytest.raises(PulsarError) as exc:
            store.rebind(bundle)
    finally:
        os.close(fd)
    assert exc.value.code == "lock_timeout" and exc.value.detail["holder"] is None
    assert "an unknown holder" in exc.value.message


def test_the_holder_record_is_written_after_acquiring(store, bundle):
    store.save(bundle)
    with store.refresh_lock_sync(1, purpose="login"):
        record = json.loads(store.lock_file.read_text())
    assert record["pid"] == os.getpid() and record["label"] == "login of x:constworks"


def test_refresh_keeps_the_binding_id(store, bundle):
    stored = store.rebind(bundle)
    assert stored.binding_id and store.load().binding_id == stored.binding_id


def _encrypt(paths, store, raw: bytes) -> None:
    key = paths.key_file.read_bytes().strip()
    write_private_atomic(store.token_file, Fernet(key).encrypt(raw))


def test_a_bundle_from_a_newer_pulsar_is_unreadable_and_kept(store, authed, paths):
    newer = {**dataclasses.asdict(authed), "sender_constrained": True}
    _encrypt(paths, store, json.dumps(newer).encode())
    before = store.token_file.read_bytes()
    with pytest.raises(PulsarError) as exc:
        store.load()
    assert exc.value.code == "credentials_unreadable"
    assert "newer pulsar" in exc.value.message and "sender_constrained" in exc.value.message
    assert store.token_file.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'{"access_token": "a"}',
        b'{"access_token": 1, "refresh_token": null,'
        b' "expires_at": 1, "scope": "", "client_id": "c"}',
    ],
)
def test_a_corrupt_bundle_is_unreadable_not_auth_expired(store, authed, paths, raw):
    _encrypt(paths, store, raw)
    with pytest.raises(PulsarError) as exc:
        store.load()
    assert exc.value.code == "credentials_unreadable" and "corrupt" in exc.value.message


def test_credential_conflict_is_a_retryable_internal_error():
    err = CredentialConflict()
    assert err.code == "internal" and err.retryable is True


# -- symlinks are refused, never followed -----------------------------


@pytest.mark.parametrize("target", ["key_file", "token_file", "accounts_file"])
def test_a_symlinked_state_file_is_refused(store, authed, paths, tmp_path, target):
    from pulsar.accounts import AccountRegistry

    path = getattr(store, target, None) or getattr(paths, target)
    elsewhere = tmp_path / f"elsewhere-{path.name}"
    os.replace(path, elsewhere)
    path.symlink_to(elsewhere)
    registry = AccountRegistry(paths)
    attempts = (
        (registry.accounts, lambda: registry.mark_status("x:constworks", "revoked"))
        if target == "accounts_file"
        else (store.load, lambda: store.save(authed))
    )
    for attempt in attempts:
        with pytest.raises(PulsarError) as exc:
            attempt()
        assert exc.value.code == "insecure_storage" and "is a symlink" in exc.value.message
        assert str(path) in exc.value.message and str(elsewhere) in exc.value.detail["fix"]


def test_a_symlinked_home_is_refused_with_the_real_directory(paths, bundle, tmp_path):
    real = tmp_path / "real-home"
    FernetFileStore.for_account(Paths(real), "x:constworks").save(bundle)
    paths.home.symlink_to(real)
    store = FernetFileStore.for_account(paths, "x:constworks")
    for attempt in (store.load, lambda: store.save(bundle), paths.ensure):
        with pytest.raises(PulsarError) as exc:
            attempt()
        assert exc.value.code == "insecure_storage" and "is a symlink" in exc.value.message
        assert (
            exc.value.detail["fix"]
            == f"point PULSAR_HOME at the real directory: PULSAR_HOME={real}"
        )


def test_writes_never_follow_a_symlink(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    for name, write in (
        ("atomic", lambda p: write_private_atomic(p, b"x")),
        ("append", lambda p: fsutil.append_private(p, "x")),
    ):
        link = tmp_path / name
        link.symlink_to(victim)
        with pytest.raises(PulsarError) as exc:
            write(link)
        assert exc.value.code == "insecure_storage"
        assert link.is_symlink() and victim.read_text() == "untouched"


def test_a_symlinked_lock_file_is_refused(store, bundle, tmp_path):
    store.save(bundle)
    victim = tmp_path / "victim"
    victim.write_text("untouched")
    store.lock_file.symlink_to(victim)
    with pytest.raises(PulsarError) as exc:
        store.rebind(bundle)
    assert exc.value.code == "insecure_storage" and victim.read_text() == "untouched"


# -- durable writes ---------------------------------------------------------


def test_publish_new_private_creates_once_and_fsyncs_the_parent(tmp_path, monkeypatch):
    synced: list[str] = []
    real = fsutil.fsync_dir
    monkeypatch.setattr(fsutil, "fsync_dir", lambda p: (synced.append(str(p)), real(p)))
    target = tmp_path / "key"
    fsutil.publish_new_private(target, b"first")
    with pytest.raises(FileExistsError):
        fsutil.publish_new_private(target, b"second")
    assert target.read_bytes() == b"first" and stat.S_IMODE(os.stat(target).st_mode) == 0o600
    assert synced == [str(tmp_path)] * 2
    assert [p.name for p in tmp_path.iterdir()] == ["key"], "no temp file left behind"


def test_ensure_private_dir_fsyncs_the_parent_of_what_it_creates(tmp_path, monkeypatch):
    synced: list[str] = []
    real = fsutil.fsync_dir
    monkeypatch.setattr(fsutil, "fsync_dir", lambda p: (synced.append(str(p)), real(p)))
    fsutil.ensure_private_dir(tmp_path / "a" / "b")
    assert synced == [str(tmp_path), str(tmp_path / "a")]
    for d in (tmp_path / "a", tmp_path / "a" / "b"):
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    synced.clear()
    fsutil.ensure_private_dir(tmp_path / "a" / "b")
    assert synced == [], "nothing created, nothing to sync"


def test_atomic_write_closes_its_temp_file_when_fchmod_fails(tmp_path, monkeypatch):
    opened_before = len(os.listdir("/proc/self/fd"))

    def fail(*_args):
        raise OSError("fchmod refused")

    monkeypatch.setattr(fsutil.os, "fchmod", fail)
    with pytest.raises(OSError, match="fchmod refused"):
        write_private_atomic(tmp_path / "f", b"x")
    monkeypatch.undo()
    assert len(os.listdir("/proc/self/fd")) == opened_before, "the temp fd leaked"
    assert list(tmp_path.iterdir()) == [], "the temp file was removed"
