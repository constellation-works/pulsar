"""Operator settings: ``config.toml`` in the pulsar home, all keys optional.

```toml
[prices]            # USD per post; X's price list changes, so it is config
plain_post_usd = 0.015
url_post_usd = 0.20

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none, path uploads off
```

Nothing in here is a secret. Unknown keys are refused so a typo cannot
silently fall back to a default.

Without ``[media] roots``, ``upload_media`` accepts only ``base64``: a path
upload publishes a local file, and the server's cwd (``/`` or ``$HOME`` under
some MCP hosts) is no safe default. A root must be absolute and may not be
``/``, the user's home, or an ancestor of it; name the directory the media
actually lives in.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import INVALID_CONFIG, PulsarError
from .jsonx import as_list, as_object
from .paths import Paths

DEFAULT_PLAIN_POST_USD = 0.015
DEFAULT_URL_POST_USD = 0.20


@dataclass(frozen=True)
class Prices:
    plain_post_usd: float = DEFAULT_PLAIN_POST_USD
    url_post_usd: float = DEFAULT_URL_POST_USD

    def for_post(self, *, has_url: bool) -> float:
        return self.url_post_usd if has_url else self.plain_post_usd


@dataclass(frozen=True)
class Settings:
    prices: Prices = field(default_factory=Prices)
    # Empty means path uploads are off (base64 only).
    media_roots: tuple[Path, ...] = ()


def _table(data: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    section = as_object(data.get(key, {}))
    if section is None:
        raise PulsarError(INVALID_CONFIG, f"config.toml: [{key}] must be a table")
    unknown = set(section) - allowed
    if unknown:
        raise PulsarError(
            INVALID_CONFIG, f"config.toml: unknown keys in [{key}]: {sorted(unknown)}"
        )
    return section


def _price(section: dict[str, Any], key: str, default: float) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise PulsarError(INVALID_CONFIG, f"config.toml: prices.{key} must be a number >= 0")
    return float(value)


def _media_root(raw: str) -> Path:
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise PulsarError(INVALID_CONFIG, f"config.toml: media root {raw!r} must be absolute")
    resolved = root.resolve()
    if Path.home().resolve().is_relative_to(resolved):
        raise PulsarError(
            INVALID_CONFIG,
            f"config.toml: media root {raw!r} is / or the home directory (or above it); "
            "name the directory the media lives in",
        )
    return root


def parse_settings(data: dict[str, Any]) -> Settings:
    unknown = set(data) - {"prices", "media"}
    if unknown:
        raise PulsarError(INVALID_CONFIG, f"config.toml: unknown sections: {sorted(unknown)}")
    prices = _table(data, "prices", {"plain_post_usd", "url_post_usd"})
    media = _table(data, "media", {"roots"})
    raw_roots: object = media.get("roots", [])
    entries = as_list(raw_roots)
    roots = [r for r in entries if isinstance(r, str) and r]
    if not isinstance(raw_roots, list) or len(roots) != len(entries):
        raise PulsarError(INVALID_CONFIG, "config.toml: media.roots must be a list of paths")
    return Settings(
        prices=Prices(
            plain_post_usd=_price(prices, "plain_post_usd", DEFAULT_PLAIN_POST_USD),
            url_post_usd=_price(prices, "url_post_usd", DEFAULT_URL_POST_USD),
        ),
        media_roots=tuple(_media_root(r) for r in roots),
    )


def load_settings(paths: Paths) -> Settings:
    if not paths.settings_file.exists():
        return Settings()
    try:
        data = tomllib.loads(paths.settings_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PulsarError(INVALID_CONFIG, f"config.toml is not valid TOML: {exc}") from exc
    return parse_settings(data)
