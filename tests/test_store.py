import json
import os
import stat

from pulsar.store import TokenBundle


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
