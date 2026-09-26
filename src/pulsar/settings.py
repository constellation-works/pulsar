"""Operator settings: ``config.toml`` in the pulsar home, all keys optional.

```toml
[prices]            # USD per post; X's price list changes, so it is config
plain_post_usd = 0.015
url_post_usd = 0.20

[media]
roots = ["~/workspace/constellation/marketing"]   # default: the caller's cwd
```

Nothing in here is a secret. Unknown keys are refused so a typo cannot
silently fall back to a default.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Paths
from .errors import INVALID_CONFIG, PulsarError

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
    # Empty means "the process cwd at call time".
    media_roots: tuple[Path, ...] = ()

    def effective_media_roots(self) -> tuple[Path, ...]:
        return self.media_roots or (Path.cwd(),)


def _table(data: dict[str, Any], key: str, allowed: set[str]) -> dict[str, Any]:
    section = data.get(key, {})
    if not isinstance(section, dict):
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


def parse_settings(data: dict[str, Any]) -> Settings:
    unknown = set(data) - {"prices", "media"}
    if unknown:
        raise PulsarError(INVALID_CONFIG, f"config.toml: unknown sections: {sorted(unknown)}")
    prices = _table(data, "prices", {"plain_post_usd", "url_post_usd"})
    media = _table(data, "media", {"roots"})
    roots = media.get("roots", [])
    if not isinstance(roots, list) or not all(isinstance(r, str) and r for r in roots):
        raise PulsarError(INVALID_CONFIG, "config.toml: media.roots must be a list of paths")
    return Settings(
        prices=Prices(
            plain_post_usd=_price(prices, "plain_post_usd", DEFAULT_PLAIN_POST_USD),
            url_post_usd=_price(prices, "url_post_usd", DEFAULT_URL_POST_USD),
        ),
        media_roots=tuple(Path(r).expanduser() for r in roots),
    )


def load_settings(paths: Paths) -> Settings:
    if not paths.settings_file.exists():
        return Settings()
    try:
        data = tomllib.loads(paths.settings_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise PulsarError(INVALID_CONFIG, f"config.toml is not valid TOML: {exc}") from exc
    return parse_settings(data)
