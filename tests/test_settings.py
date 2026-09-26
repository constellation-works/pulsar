import os
import stat

import pytest

from pulsar.core.errors import PulsarError
from pulsar.core.fsutil import append_private, write_private_atomic
from pulsar.core.settings import Prices, Settings, load_settings


def test_defaults_without_a_config_file(paths):
    s = load_settings(paths)
    assert s == Settings()
    assert s.prices.for_post(has_url=False) == 0.015
    assert s.prices.for_post(has_url=True) == 0.20


def test_prices_and_media_roots_from_config(paths, tmp_path):
    paths.ensure()
    paths.settings_file.write_text(
        f'[prices]\nplain_post_usd = 0.02\nurl_post_usd = 0.3\n[media]\nroots = ["{tmp_path}"]\n'
    )
    s = load_settings(paths)
    assert s.prices == Prices(plain_post_usd=0.02, url_post_usd=0.3)
    assert s.prices_for("x") == s.prices
    assert s.media_roots == (tmp_path,)


@pytest.mark.parametrize(
    "body",
    [
        "[prices]\nplain_post_usd = -1\n",
        "[prices]\nplain_post_usd = true\n",
        "[prices]\nplian_post_usd = 0.1\n",
        "[budget]\ndaily = 1\n",
        '[media]\nroots = "/tmp"\n',
        "not toml [",
    ],
)
def test_bad_config_is_refused_not_defaulted(paths, body):
    paths.ensure()
    paths.settings_file.write_text(body)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"


def test_no_media_roots_by_default(paths):
    assert load_settings(paths).media_roots == ()


@pytest.mark.parametrize("root", ["/", "~", "~/..", "relative/media"])
def test_broad_or_relative_media_roots_are_refused(paths, root):
    paths.ensure()
    paths.settings_file.write_text(f'[media]\nroots = ["{root}"]\n')
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"


def test_a_directory_below_home_is_an_acceptable_root(paths, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    paths.ensure()
    paths.settings_file.write_text('[media]\nroots = ["~/marketing"]\n')
    assert load_settings(paths).media_roots == (tmp_path / "marketing",)


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_private_writes_are_0600_regardless_of_umask(tmp_path):
    old = os.umask(0o002)
    try:
        write_private_atomic(tmp_path / "a", b"x")
        append_private(tmp_path / "b", "line\n")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "a") == 0o600
    assert _mode(tmp_path / "b") == 0o600


def test_atomic_write_leaves_the_old_file_when_the_write_fails(tmp_path, monkeypatch):
    target = tmp_path / "tokens.enc"
    write_private_atomic(target, b"old")

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_private_atomic(target, b"new")
    assert target.read_bytes() == b"old"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tokens.enc"], "temp file cleaned up"


def test_full_config(paths, tmp_path, monkeypatch):
    from datetime import time

    monkeypatch.setenv("HOME", str(tmp_path))
    paths.ensure()
    paths.settings_file.write_text(
        """
default_account = "X:@ConstWorks"

[accounts."x:constworks"]
expected_handle = "@ConstWorks"

[prices.x]
plain_post_usd = 0.02

[prices.bsky]
plain_post_usd = 0
url_post_usd = 0

[policy]
daily_budget_usd = 0.5
monthly_budget_usd = 4
max_posts_per_day = 3
quiet_hours = "23:00-07:30"
timezone = "America/Los_Angeles"

[media]
roots = ["~/marketing"]
"""
    )
    s = load_settings(paths)
    assert s.default_account == "x:constworks"
    assert s.account_config("x:constworks").expected_handle == "constworks"
    assert s.prices_for("x") == Prices(plain_post_usd=0.02, url_post_usd=0.20)
    assert s.prices_for("bsky") == Prices(0.0, 0.0)
    assert s.prices_for("mastodon") == Prices(0.0, 0.0)
    assert s.policy.daily_budget_usd == 0.5 and s.policy.monthly_budget_usd == 4.0
    assert s.policy.max_posts_per_day == 3
    assert s.policy.quiet_hours == (time(23, 0), time(7, 30))
    assert s.policy.tz.key == "America/Los_Angeles"


def test_policy_defaults_agreed_with_daniel():
    p = Settings().policy
    assert (p.daily_budget_usd, p.monthly_budget_usd, p.max_posts_per_day) == (1.0, 10.0, 5)
    assert p.quiet_hours is None and p.timezone == "UTC"


@pytest.mark.parametrize(
    "body",
    [
        '[policy]\nquiet_hours = "late"\n',
        '[policy]\nquiet_hours = "07:00-07:00"\n',
        '[policy]\nquiet_hours = "25:00-07:00"\n',
        '[policy]\ntimezone = "Mars/Olympus"\n',
        "[policy]\nmax_posts_per_day = 2.5\n",
        "[policy]\ndaily_budget_usd = -1\n",
        '[policy]\nquiet = "23:00-07:00"\n',
        'default_account = "constworks"\n',
        '[accounts."x:a"]\nhandle = "a"\n',
        "[prices]\nplain_post_usd = 0.1\n[prices.x]\nplain_post_usd = 0.2\n",
        "[prices.x]\npost_usd = 0.1\n",
    ],
)
def test_bad_policy_and_account_config_is_refused(paths, body):
    paths.ensure()
    paths.settings_file.write_text(body)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"
