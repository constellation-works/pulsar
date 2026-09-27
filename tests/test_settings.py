import os
import stat

import pytest

from pulsar.app.core.channels.contract import Prices
from pulsar.app.settings import Settings, load_settings
from pulsar.internal.errors import PulsarError
from pulsar.internal.fs import Paths, append_private, write_private_atomic


@pytest.fixture
def user_paths(paths, tmp_path) -> Paths:
    """``paths`` with the user's home resolved, as a surface hands it down."""
    user = tmp_path / "user"
    user.mkdir()
    return Paths(paths.home, user_home=user)


def _config(paths, text, mode=0o644):
    """Write config.toml the way an operator's editor does (readable, not writable)."""
    paths.ensure()
    paths.settings_file.write_text(text)
    os.chmod(paths.settings_file, mode)


def test_defaults_without_a_config_file(paths):
    s = load_settings(paths)
    assert s == Settings()
    assert s.prices.for_post(has_url=False) == 0.015
    assert s.prices.for_post(has_url=True) == 0.20


def test_prices_and_media_roots_from_config(paths, tmp_path):
    _config(
        paths,
        f'[prices]\nplain_post_usd = 0.02\nurl_post_usd = 0.3\n[media]\nroots = ["{tmp_path}"]\n',
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
    _config(paths, body)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"


def test_no_media_roots_by_default(paths):
    assert load_settings(paths).media_roots == ()


@pytest.mark.parametrize("root", ["/", "~", "~/..", "relative/media", "~other/media"])
def test_broad_or_relative_media_roots_are_refused(user_paths, root):
    _config(user_paths, f'[media]\nroots = ["{root}"]\n')
    with pytest.raises(PulsarError) as exc:
        load_settings(user_paths)
    assert exc.value.code == "invalid_config"
    assert exc.value.detail == {"path": str(user_paths.settings_file), "key": "media.roots"}


def test_the_user_home_itself_is_refused_as_a_root(user_paths):
    _config(user_paths, f'[media]\nroots = ["{user_paths.user_home}"]\n')
    with pytest.raises(PulsarError) as exc:
        load_settings(user_paths)
    assert "home directory" in exc.value.message


def test_a_directory_below_home_is_an_acceptable_root(user_paths, monkeypatch):
    monkeypatch.setenv("HOME", "/nonexistent")  # core never reads it
    _config(user_paths, '[media]\nroots = ["~/marketing"]\n')
    assert load_settings(user_paths).media_roots == (user_paths.user_home / "marketing",)


def test_tilde_needs_a_user_home_from_the_caller(paths, tmp_path):
    _config(paths, '[media]\nroots = ["~/marketing"]\n')
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config" and "absolute path" in exc.value.message
    assert load_settings(paths, user_home=tmp_path).media_roots == (tmp_path / "marketing",)


# -- R28: numbers are checked at load, naming the key and the resolved file -----------


@pytest.mark.parametrize(
    ("body", "key"),
    [
        ("[policy]\ndaily_budget_usd = nan\n", "policy.daily_budget_usd"),
        ("[policy]\nmonthly_budget_usd = inf\n", "policy.monthly_budget_usd"),
        ("[policy]\ndaily_budget_usd = -inf\n", "policy.daily_budget_usd"),
        ("[prices.x]\nplain_post_usd = nan\n", "prices.x.plain_post_usd"),
        ("[prices]\nurl_post_usd = inf\n", "prices.url_post_usd"),
        ("[prices.x]\nurl_post_usd = 101\n", "prices.x.url_post_usd"),
        ("[policy]\ndaily_budget_usd = 1e308\n", "policy.daily_budget_usd"),
        ("[policy]\nmonthly_budget_usd = 100001\n", "policy.monthly_budget_usd"),
        ("[policy]\nmax_posts_per_day = 10001\n", "policy.max_posts_per_day"),
        ("[policy]\nmax_posts_per_day = 5.0\n", "policy.max_posts_per_day"),
        ('[policy]\nmax_posts_per_day = "5"\n', "policy.max_posts_per_day"),
    ],
)
def test_numbers_must_be_finite_bounded_and_the_right_kind(paths, body, key):
    _config(paths, body)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"
    assert exc.value.message.startswith(f"{paths.settings_file}: {key} must be")
    assert exc.value.detail == {"path": str(paths.settings_file), "key": key}


def test_bounds_are_inclusive(paths):
    _config(
        paths,
        "[prices.x]\nplain_post_usd = 100\n[policy]\ndaily_budget_usd = 10000\n"
        "monthly_budget_usd = 100000\nmax_posts_per_day = 10000\n",
    )
    s = load_settings(paths)
    assert s.prices.plain_post_usd == 100.0 and s.policy.max_posts_per_day == 10_000


def test_errors_name_the_resolved_config_path(paths):
    _config(paths, "not toml [")
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.message.startswith(f"{paths.settings_file}: not valid TOML")


# -- R9: the config is refused when others could have changed it ------------------------


@pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
def test_a_group_or_world_writable_config_is_refused(paths, mode):
    _config(paths, "[policy]\ndaily_budget_usd = 1000\n", mode=mode)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "insecure_storage"
    assert str(paths.settings_file) in exc.value.message
    assert exc.value.detail["fix"] == f"chmod go-w {paths.settings_file}"


def test_a_readable_config_is_fine(paths):
    _config(paths, "[policy]\ndaily_budget_usd = 2\n", mode=0o644)
    assert load_settings(paths).policy.daily_budget_usd == 2.0


def test_a_symlinked_config_is_refused(paths, tmp_path):
    real = tmp_path / "dotfiles-config.toml"
    real.write_text("[policy]\ndaily_budget_usd = 1000\n")
    os.chmod(real, 0o600)
    paths.ensure()
    paths.settings_file.symlink_to(real)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "insecure_storage" and "symlink" in exc.value.message
    assert str(real) in exc.value.detail["fix"]


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


def test_full_config(user_paths):
    from datetime import time

    paths = user_paths
    _config(
        paths,
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
""",
    )
    s = load_settings(paths)
    assert s.default_account == "x:constworks"
    assert s.account_config("x:constworks").expected_handle == "constworks"
    assert s.prices_for("x") == Prices(plain_post_usd=0.02, url_post_usd=0.20)
    assert s.prices_for("bsky") == Prices(0.0, 0.0)
    assert s.prices_for("mastodon") == Prices(0.0, 0.0, 0.0), "an unpriced provider is free"
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
    _config(paths, body)
    with pytest.raises(PulsarError) as exc:
        load_settings(paths)
    assert exc.value.code == "invalid_config"
