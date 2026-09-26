"""Account health: what ``pulsar auth status`` and ``pulsar.status`` report.

Health is three-valued (unknown never reads as healthy):

- ``healthy``: bound, active, the stored binding's identity is known and
  matches the alias, and the access token is still valid (or ``--live``
  just proved the refresh).
- ``unverified``: nothing is known to be wrong, but a check could not run
  offline: no identity is cached for this binding, or the access token has
  expired and the refresh has not been exercised. ``--live`` settles it.
- ``unhealthy``: something is known to be wrong (not bound, logged out,
  re-authorization required, a handle mismatch, unreadable storage).

The default report reads only local state: no network, no writes.
``live`` is the explicit check that costs a refresh (which rotates the
token pair) and a ``/users/me`` read, and records what it proved.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Literal, assert_never

from pulsar.core import (
    ACTIVE,
    AUTH_EXPIRED,
    REAUTH_REQUIRED,
    REVOKED,
    Account,
    AccountRegistry,
    PulsarError,
    Settings,
    check_handle,
    expected_handles,
    home_command,
    login_command,
)
from pulsar.providers.x import REFRESH_AHEAD_SECONDS, load_client_id

from .runtime import Runtime

Health = Literal["healthy", "unverified", "unhealthy"]
TokenState = Literal["valid", "expiring", "expired"]

Report = tuple[dict[str, Any], int]


def token_state(expires_in_s: int) -> TokenState:
    if expires_in_s <= 0:
        return "expired"
    return "expiring" if expires_in_s <= REFRESH_AHEAD_SECONDS else "valid"


def attention(entry: dict[str, Any], home: Path) -> str | None:
    """What an operator should do about one account entry, or None when
    healthy. Commands name ``home``: the plugin's is not the operator's default."""
    alias = entry["alias"]
    health: Health = entry["health"]
    match health:
        case "healthy":
            return None
        case "unverified":
            return (
                f"{alias}: not verified offline ({entry['reason']}); run "
                f"`{home_command(home, f'auth status --live --account {alias}')}`"
            )
        case "unhealthy":
            if entry["reauth_required"] or not entry["authorized"]:
                return f"{alias}: re-authorization required (`{login_command(home, alias)}`)"
            return f"{alias}: {entry['reason']}"
        case _:
            assert_never(health)


async def auth_report(rt: Runtime, *, account: str | None = None, live: bool = False) -> Report:
    """Every registered account (or just ``account``) and its health; exit 0
    only when every reported account is ``healthy``. Raises ``PulsarError``
    when the home itself cannot be read. Only ``live`` uses ``rt``'s clients."""
    paths, settings, registry = rt.paths, rt.settings, rt.registry
    out: dict[str, Any] = {
        "home": str(paths.home),
        "client_id": None,
        "default_account": None,
        "legacy": None,
        "accounts": [],
    }
    out["client_id"] = load_client_id(paths)
    legacy = registry.legacy_status(settings)
    if legacy.state != "none":
        out["legacy"] = {"state": legacy.state, "alias": legacy.alias, "message": legacy.message}
    out["default_account"] = settings.default_account
    if account is not None:
        targets = [registry.resolve(account, settings)]
    else:
        rows = registry.accounts()
        targets = [rows[alias] for alias in sorted(rows)]
    for target in targets:
        out["accounts"].append(await _account(registry, settings, target, rt if live else None))
    ok = bool(targets) and all(entry["health"] == "healthy" for entry in out["accounts"])
    return out, 0 if ok else 1


async def _account(
    registry: AccountRegistry, settings: Settings, account: Account, rt: Runtime | None
) -> dict[str, Any]:
    alias = account.alias
    entry: dict[str, Any] = {
        "alias": alias,
        "status": account.status,
        "expected_handle": expected_handles(alias, settings)[-1],
        "authorized": False,
        "reauth_required": True,
        "access_token_expires_in_s": None,
        "token_state": None,
        "scope": None,
        "account": None,
        "account_source": None,
        "verified": False,
        "refreshed": False,
        "mismatch": None,
        "error": None,
    }
    try:
        bundle = registry.store(alias).load()
    except PulsarError as exc:  # insecure or unreadable storage: not a re-login
        entry["error"] = exc.to_result()
        return _finish(entry, registry, settings)
    entry["authorized"] = bundle is not None
    if bundle is None:
        return _finish(entry, registry, settings)
    expires_in = int(bundle.expires_at - time.time())
    entry.update(
        access_token_expires_in_s=expires_in,
        token_state=token_state(expires_in),
        scope=bundle.scope,
        reauth_required=not bundle.refresh_token,
    )
    cached = registry.trusted_identity(account, bundle)
    if cached is not None:
        entry["account"] = {"user_id": cached.provider_user_id, "username": cached.handle}
        entry["account_source"] = "cache"
    if rt is None:
        return _finish(entry, registry, settings)
    try:
        async with rt.watch_expiry(alias):
            fresh = await rt.client_for(alias).refresh(bundle)
        entry["refreshed"] = True
        entry["access_token_expires_in_s"] = int(fresh.expires_at - time.time())
        entry["token_state"] = token_state(entry["access_token_expires_in_s"])
        found = await rt.identity(alias, live=True)
        if account.status == REAUTH_REQUIRED:
            registry.mark_status(alias, ACTIVE)  # the refresh token works again
        entry["account"] = {"user_id": found.provider_user_id, "username": found.handle}
        entry["account_source"] = "live"
        entry["verified"] = True
    except PulsarError as exc:
        entry["error"] = exc.to_result()
        if exc.code == AUTH_EXPIRED:
            entry["reauth_required"] = True
    return _finish(entry, registry, settings)


def _finish(entry: dict[str, Any], registry: AccountRegistry, settings: Settings) -> dict[str, Any]:
    alias = entry["alias"]
    current = registry.accounts().get(alias)
    entry["status"] = current.status if current is not None else entry["status"]
    if entry["status"] in (REAUTH_REQUIRED, REVOKED):
        entry["reauth_required"] = True
    if entry["account"] is not None:
        try:
            check_handle(alias, entry["account"]["username"], settings)
            entry["mismatch"] = False
        except PulsarError:
            entry["mismatch"] = True
    entry["health"], entry["reason"] = _health(entry)
    entry["healthy"] = entry["health"] == "healthy"
    return entry


def _health(entry: dict[str, Any]) -> tuple[Health, str | None]:
    error = entry["error"]
    if error is not None and not error["retryable"]:
        return "unhealthy", str(error["message"])
    if not entry["authorized"]:
        return "unhealthy", "no credentials stored"
    if entry["reauth_required"]:
        return "unhealthy", "re-authorization required"
    if entry["status"] != ACTIVE:
        return "unhealthy", f"account is {entry['status']}"
    if entry["mismatch"]:
        return "unhealthy", "the stored credentials belong to another handle"
    if error is not None:
        # A transient failure (network, rate limit, a held lock): the live
        # check did not run, which proves nothing either way.
        return "unverified", f"the live check could not run: {error['message']}"
    if entry["mismatch"] is None:
        return "unverified", "no identity cached for the stored credentials"
    if entry["token_state"] == "expired" and not entry["refreshed"]:
        return "unverified", "the access token has expired and the refresh was not exercised"
    return "healthy", None
