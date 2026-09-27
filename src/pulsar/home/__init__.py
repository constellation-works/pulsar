"""The pulsar home directory: where things live under it (``paths``), how
files there are written privately and locked (``files``), and the operator's
``config.toml`` (``settings``).
"""

from __future__ import annotations

from .files import (
    append_private,
    ensure_private_dir,
    fsync_dir,
    hold_lock,
    hold_lock_async,
    publish_new_private,
    require_private,
    text_sha256,
    write_private_atomic,
)
from .paths import Paths, account_slug, alias_from_slug, resolve_home
from .settings import (
    DEFAULT_PLAIN_POST_USD,
    DEFAULT_URL_POST_USD,
    AccountConfig,
    PolicyConfig,
    Prices,
    Settings,
    load_settings,
)

__all__ = [
    # files
    "append_private",
    "ensure_private_dir",
    "fsync_dir",
    "hold_lock",
    "hold_lock_async",
    "publish_new_private",
    "require_private",
    "text_sha256",
    "write_private_atomic",
    # paths
    "account_slug",
    "alias_from_slug",
    "Paths",
    "resolve_home",
    # settings
    "DEFAULT_PLAIN_POST_USD",
    "DEFAULT_URL_POST_USD",
    "AccountConfig",
    "load_settings",
    "PolicyConfig",
    "Prices",
    "Settings",
]
