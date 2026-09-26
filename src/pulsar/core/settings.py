"""Operator settings: ``config.toml`` in the pulsar home, all keys optional.

```toml
default_account = "x:constworks"       # used when a call names no account

[accounts."x:constworks"]
expected_handle = "constworks"         # the bound handle must match, else account_mismatch

[prices.x]                             # USD per post; price lists change, so they are config
plain_post_usd = 0.015
url_post_usd = 0.20

[policy]                               # enforced before any network call
daily_budget_usd = 1.0                 # 0 stops all paid publishing
monthly_budget_usd = 10.0
max_posts_per_day = 5                  # per account; each post of a thread counts
quiet_hours = "23:00-07:00"            # optional; no publishing inside the window
timezone = "UTC"                       # the day/month boundary and quiet hours clock

[media]
roots = ["~/workspace/constellation/marketing"]   # default: none, path uploads off
```

The flat ``[prices]`` keys of the first config format (``plain_post_usd``,
``url_post_usd`` directly under ``[prices]``) still mean X's prices.

Nothing in here is a secret. Unknown keys are refused so a typo cannot
silently fall back to a default.

Without ``[media] roots``, path media are refused and only ``base64`` uploads
work: a path upload publishes a local file, and the server's cwd (``/`` or
``$HOME`` under some MCP hosts) is no safe default. A root must be absolute
and may not be ``/``, the user's home, or an ancestor of it; name the
directory the media actually lives in.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import INVALID_CONFIG, PulsarError
from .jsonx import as_list, as_object
from .paths import Paths
from .plan import normalize_alias

DEFAULT_PLAIN_POST_USD = 0.015
DEFAULT_URL_POST_USD = 0.20
# Agreed with Daniel on 2026-09-26: about 1 post a day today, so these leave
# room for a launch day while stopping a runaway loop within a day.
DEFAULT_DAILY_BUDGET_USD = 1.0
DEFAULT_MONTHLY_BUDGET_USD = 10.0
DEFAULT_MAX_POSTS_PER_DAY = 5

_QUIET_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class Prices:
    plain_post_usd: float = DEFAULT_PLAIN_POST_USD
    url_post_usd: float = DEFAULT_URL_POST_USD

    def for_post(self, *, has_url: bool) -> float:
        return self.url_post_usd if has_url else self.plain_post_usd


FREE = Prices(plain_post_usd=0.0, url_post_usd=0.0)


@dataclass(frozen=True)
class AccountConfig:
    alias: str
    expected_handle: str | None = None


@dataclass(frozen=True)
class PolicyConfig:
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD
    monthly_budget_usd: float = DEFAULT_MONTHLY_BUDGET_USD
    max_posts_per_day: int = DEFAULT_MAX_POSTS_PER_DAY
    quiet_hours: tuple[time, time] | None = None  # [start, end); may wrap midnight
    timezone: str = "UTC"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class Settings:
    # (provider, prices), sorted. X has built-in defaults; a provider with no
    # entry is priced at zero.
    provider_prices: tuple[tuple[str, Prices], ...] = (("x", Prices()),)
    # Empty means path uploads are off (base64 only).
    media_roots: tuple[Path, ...] = ()
    default_account: str | None = None
    accounts: tuple[AccountConfig, ...] = ()
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    def prices_for(self, provider: str) -> Prices:
        return dict(self.provider_prices).get(provider, FREE)

    @property
    def prices(self) -> Prices:
        """X's prices, for the single-provider call sites."""
        return self.prices_for("x")

    def account_config(self, alias: str) -> AccountConfig | None:
        return next((a for a in self.accounts if a.alias == alias), None)


def _fail(message: str) -> PulsarError:
    return PulsarError(INVALID_CONFIG, f"config.toml: {message}")


def _table(data: dict[str, Any], key: str, allowed: set[str] | None, where: str) -> dict[str, Any]:
    section = as_object(data.get(key, {}))
    if section is None:
        raise _fail(f"[{where}] must be a table")
    if allowed is not None:
        unknown = set(section) - allowed
        if unknown:
            raise _fail(f"unknown keys in [{where}]: {sorted(unknown)}")
    return section


def _number(section: dict[str, Any], key: str, default: float, where: str) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise _fail(f"{where}.{key} must be a number >= 0")
    return float(value)


def _prices(section: dict[str, Any], where: str) -> Prices:
    return Prices(
        plain_post_usd=_number(section, "plain_post_usd", DEFAULT_PLAIN_POST_USD, where),
        url_post_usd=_number(section, "url_post_usd", DEFAULT_URL_POST_USD, where),
    )


def _parse_prices(data: dict[str, Any]) -> tuple[tuple[str, Prices], ...]:
    section = _table(data, "prices", None, "prices")
    flat = {k: v for k, v in section.items() if not isinstance(v, dict)}
    nested = [k for k in section if as_object(section[k]) is not None]
    unknown_flat = set(flat) - {"plain_post_usd", "url_post_usd"}
    if unknown_flat:
        raise _fail(f"unknown keys in [prices]: {sorted(unknown_flat)}")
    if flat and "x" in nested:
        raise _fail("give X's prices either flat under [prices] or in [prices.x], not both")
    table: dict[str, Prices] = {"x": _prices(flat, "prices")}
    for provider in nested:
        sub = _table(section, provider, {"plain_post_usd", "url_post_usd"}, f"prices.{provider}")
        table[provider.lower()] = _prices(sub, f"prices.{provider}")
    return tuple(sorted(table.items()))


def _media_root(raw: str) -> Path:
    root = Path(raw).expanduser()
    if not root.is_absolute():
        raise _fail(f"media root {raw!r} must be absolute")
    resolved = root.resolve()
    if Path.home().resolve().is_relative_to(resolved):
        raise _fail(
            f"media root {raw!r} is / or the home directory (or above it); "
            "name the directory the media lives in"
        )
    return root


def _parse_media(data: dict[str, Any]) -> tuple[Path, ...]:
    media = _table(data, "media", {"roots"}, "media")
    raw_roots: object = media.get("roots", [])
    entries = as_list(raw_roots)
    roots = [r for r in entries if isinstance(r, str) and r]
    if not isinstance(raw_roots, list) or len(roots) != len(entries):
        raise _fail("media.roots must be a list of paths")
    return tuple(_media_root(r) for r in roots)


def _alias(raw: object, where: str) -> str:
    if not isinstance(raw, str):
        raise _fail(f"{where} must be a provider:handle string")
    try:
        return normalize_alias(raw)
    except PulsarError as exc:
        raise _fail(f"{where}: {exc.message}") from exc


def _parse_accounts(data: dict[str, Any]) -> tuple[AccountConfig, ...]:
    section = _table(data, "accounts", None, "accounts")
    out: list[AccountConfig] = []
    for raw_alias in section:
        alias = _alias(raw_alias, f"[accounts.{raw_alias!r}]")
        body = _table(section, raw_alias, {"expected_handle"}, f"accounts.{raw_alias}")
        handle = body.get("expected_handle")
        if handle is not None and (not isinstance(handle, str) or not handle.strip()):
            raise _fail(f"accounts.{raw_alias}.expected_handle must be a non-empty string")
        out.append(
            AccountConfig(
                alias=alias,
                expected_handle=handle.strip().removeprefix("@").lower() if handle else None,
            )
        )
    return tuple(sorted(out, key=lambda a: a.alias))


def _parse_quiet(raw: object) -> tuple[time, time] | None:
    if raw is None:
        return None
    match = _QUIET_RE.match(raw) if isinstance(raw, str) else None
    if match is None:
        raise _fail('policy.quiet_hours must look like "23:00-07:00"')
    h1, m1, h2, m2 = (int(g) for g in match.groups())
    try:
        start, end = time(h1, m1), time(h2, m2)
    except ValueError as exc:
        raise _fail(f"policy.quiet_hours: {exc}") from exc
    if start == end:
        raise _fail("policy.quiet_hours start and end must differ")
    return start, end


def _parse_policy(data: dict[str, Any]) -> PolicyConfig:
    allowed = {
        "daily_budget_usd",
        "monthly_budget_usd",
        "max_posts_per_day",
        "quiet_hours",
        "timezone",
    }
    section = _table(data, "policy", allowed, "policy")
    cap = section.get("max_posts_per_day", DEFAULT_MAX_POSTS_PER_DAY)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
        raise _fail("policy.max_posts_per_day must be an integer >= 0")
    tz = section.get("timezone", "UTC")
    if not isinstance(tz, str):
        raise _fail("policy.timezone must be an IANA zone name, e.g. America/Los_Angeles")
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise _fail(f"policy.timezone {tz!r} is not a known IANA zone") from exc
    return PolicyConfig(
        daily_budget_usd=_number(section, "daily_budget_usd", DEFAULT_DAILY_BUDGET_USD, "policy"),
        monthly_budget_usd=_number(
            section, "monthly_budget_usd", DEFAULT_MONTHLY_BUDGET_USD, "policy"
        ),
        max_posts_per_day=cap,
        quiet_hours=_parse_quiet(section.get("quiet_hours")),
        timezone=tz,
    )


def parse_settings(data: dict[str, Any]) -> Settings:
    unknown = set(data) - {"default_account", "accounts", "prices", "policy", "media"}
    if unknown:
        raise _fail(f"unknown keys: {sorted(unknown)}")
    default_account = data.get("default_account")
    return Settings(
        provider_prices=_parse_prices(data),
        media_roots=_parse_media(data),
        default_account=(
            _alias(default_account, "default_account") if default_account is not None else None
        ),
        accounts=_parse_accounts(data),
        policy=_parse_policy(data),
    )


def load_settings(paths: Paths) -> Settings:
    if not paths.settings_file.exists():
        return Settings()
    try:
        data = tomllib.loads(paths.settings_file.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise _fail(f"not valid TOML: {exc}") from exc
    return parse_settings(data)
