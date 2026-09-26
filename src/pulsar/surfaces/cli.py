"""``pulsar`` command line: human auth management, operator verbs (``ops.py``)
and the MCP server."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from .. import __version__
from ..core.accounts import (
    ACTIVE,
    REAUTH_REQUIRED,
    REVOKED,
    Account,
    AccountRegistry,
    check_handle,
    expected_handles,
)
from ..core.errors import AUTH_EXPIRED, PulsarError
from ..core.paths import Paths, default_paths
from ..core.settings import Settings, load_settings
from ..providers.x.auth import load_client_id, login
from ..providers.x.client import REFRESH_AHEAD_SECONDS
from ..providers.x.config import CALLBACK_HOST

if TYPE_CHECKING:
    from .mcp import Runtime


def _fail(exc: PulsarError) -> int:
    print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
    return 1


def _migrate_quietly(registry: AccountRegistry, settings: Settings) -> None:
    """First-use migration of a phase 1 home; a problem there must not block a login."""
    try:
        result = registry.migrate_legacy(settings)
    except PulsarError as exc:
        print(
            f"warning: legacy credentials not migrated [{exc.code}]: {exc.message}", file=sys.stderr
        )
        return
    if result.state == "migrated":
        print(f"migrated the legacy credentials to {result.alias}", file=sys.stderr)


def _auth_login(args: argparse.Namespace) -> int:
    paths = default_paths()
    try:
        settings = load_settings(paths)
        alias = args.account or settings.default_account
        if not alias:
            print(
                "error: --account is required (the account to bind, e.g. --account x:constworks)",
                file=sys.stderr,
            )
            return 2
        client_id = args.client_id or load_client_id(paths)
        if not client_id:
            print(
                "error: --client-id is required the first time (the X app's OAuth 2.0 client id)",
                file=sys.stderr,
            )
            return 2
        _migrate_quietly(AccountRegistry(paths), settings)
        account = login(paths, settings, alias, client_id, open_browser=not args.no_browser)
    except PulsarError as exc:
        return _fail(exc)
    # login() asked X with the new token before storing it, so this is live proof.
    out = {
        "ok": True,
        "alias": account.alias,
        "account": {"user_id": account.provider_user_id, "username": account.handle},
        "account_source": "live",
        "verified": True,
        "scope": " ".join(account.scopes) or "(unreported)",
        "home": str(paths.home),
    }
    print(json.dumps(out, indent=2))
    return 0


def _token_state(expires_in_s: int) -> str:
    if expires_in_s <= 0:
        return "expired"
    return "expiring" if expires_in_s <= REFRESH_AHEAD_SECONDS else "valid"


async def status_report(
    paths: Paths,
    *,
    account: str | None = None,
    live: bool = False,
    offline: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[dict[str, Any], int]:
    """What ``pulsar auth status`` prints, and its exit code.

    One entry per registered account (or just ``account``). The default
    reads each account's cached identity: it costs nothing, but says nothing
    about whether the refresh token still works (an expired access token is
    reported as ``verified: false``). ``live`` proves it: a forced refresh,
    which rotates the pair through that account's own lock, then ``GET
    /2/users/me`` bypassing the cache. ``offline`` makes no network call at
    all. Exit 0 only when every reported account is healthy.
    """
    out: dict[str, Any] = {
        "home": str(paths.home),
        "client_id": load_client_id(paths),
        "default_account": None,
        "accounts": [],
    }
    try:
        settings = load_settings(paths)
        registry = AccountRegistry(paths)
        migration = registry.migrate_legacy(settings)
        if migration.state == "migrated":
            out["migrated"] = migration.alias
        if migration.message:
            out["legacy"] = migration.message
        out["default_account"] = settings.default_account
        if account is not None:
            targets = [registry.resolve(account, settings)]
        else:
            rows = registry.accounts()
            targets = [rows[alias] for alias in sorted(rows)]
    except PulsarError as exc:
        out["error"] = exc.to_result()
        return out, 1
    from .mcp import Runtime

    rt = None if offline else Runtime(paths, settings=settings, transport=transport)
    try:
        for target in targets:
            out["accounts"].append(await _account_status(registry, settings, target, rt, live))
    finally:
        if rt is not None:
            await rt.aclose()
    ok = bool(targets) and all(entry["healthy"] for entry in out["accounts"])
    return out, 0 if ok else 1


async def _account_status(
    registry: AccountRegistry,
    settings: Settings,
    account: Account,
    rt: Runtime | None,
    live: bool,
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
        "mismatch": None,
    }
    try:
        bundle = registry.store(alias).load()
    except PulsarError as exc:  # insecure_storage: a chmod away, not a re-login
        entry["error"] = exc.to_result()
        return _finish(entry, registry, settings)
    entry["authorized"] = bundle is not None
    if bundle is None:
        return _finish(entry, registry, settings)
    expires_in = int(bundle.expires_at - time.time())
    entry.update(
        access_token_expires_in_s=expires_in,
        token_state=_token_state(expires_in),
        scope=bundle.scope,
        reauth_required=not bundle.refresh_token,
    )
    cached = registry.trusted_identity(account, bundle)
    if rt is None:  # offline
        if cached is not None:
            entry["account"] = {"user_id": cached.provider_user_id, "username": cached.handle}
            entry["account_source"] = "cache"
        return _finish(entry, registry, settings)
    try:
        if live:
            with rt.watch_expiry(alias):
                fresh = await rt.client_for(alias).refresh(bundle)
            entry["refreshed"] = True
            entry["access_token_expires_in_s"] = int(fresh.expires_at - time.time())
            entry["token_state"] = _token_state(entry["access_token_expires_in_s"])
            found = await rt.identity(alias, live=True)
            if account.status == REAUTH_REQUIRED:
                registry.mark_status(alias, ACTIVE)  # the refresh token works again
            entry["account_source"] = "live"
            entry["verified"] = True
            entry["whoami_cache"] = "refreshed" if cached is not None else "created"
        else:
            found = await rt.identity(alias)
            entry["account_source"] = "cache" if cached is not None else "live"
            entry["verified"] = cached is None
        entry["account"] = {"user_id": found.provider_user_id, "username": found.handle}
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
    if not entry["verified"] and not entry["reauth_required"] and entry["authorized"]:
        entry["note"] = (
            "account read from cache; refresh not exercised"
            + (" and the access token has expired" if entry["token_state"] == "expired" else "")
            + f" — run `pulsar auth status --live --account {alias}` to prove the binding"
        )
    entry["healthy"] = bool(
        entry["authorized"]
        and not entry["reauth_required"]
        and "error" not in entry
        and not entry["mismatch"]
        and entry["status"] == ACTIVE
    )
    return entry


def _auth_status(args: argparse.Namespace) -> int:
    out, code = asyncio.run(
        status_report(
            default_paths(),
            account=args.account,
            live=args.live,
            offline=getattr(args, "offline", False),
        )
    )
    print(json.dumps(out, indent=2))
    return code


def _auth_logout(args: argparse.Namespace) -> int:
    paths = default_paths()
    try:
        settings = load_settings(paths)
        registry = AccountRegistry(paths)
        _migrate_quietly(registry, settings)
        account = registry.resolve(args.account, settings)
        registry.logout(account.alias)
    except PulsarError as exc:
        return _fail(exc)
    print(f"{account.alias}: token bundle removed; account marked revoked")
    return 0


def _auth_migrate(args: argparse.Namespace) -> int:
    paths = default_paths()
    try:
        result = AccountRegistry(paths).migrate_legacy(load_settings(paths), args.account)
    except PulsarError as exc:
        return _fail(exc)
    out = {
        "state": result.state,
        "alias": result.alias,
        "adopted": list(result.adopted),
        "message": result.message
        or {
            "migrated": f"legacy credentials are now {result.alias}; run "
            f"`pulsar auth status --live --account {result.alias}` to prove the binding",
            "none": "no legacy credentials to migrate",
        }.get(result.state),
    }
    print(json.dumps(out, indent=2))
    return 0 if result.state in ("migrated", "none") else 1


def _emit(report: tuple[dict[str, Any], int]) -> int:
    out, code = report
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


def _status(args: argparse.Namespace) -> int:
    from .ops import budget_report

    return _emit(budget_report(default_paths(), account=args.account))


def _history(args: argparse.Namespace) -> int:
    from .ops import history_report

    return _emit(history_report(default_paths(), account=args.account, limit=args.limit))


def _validate(args: argparse.Namespace) -> int:
    from .ops import validate_report

    return _emit(asyncio.run(validate_report(default_paths(), args.plan, account=args.account)))


def _publish(args: argparse.Namespace) -> int:
    from .ops import publish_report

    return _emit(
        asyncio.run(
            publish_report(
                default_paths(),
                args.plan,
                account=args.account,
                idempotency_key=args.idempotency_key,
                caller=args.caller,
                yes=args.yes,
            )
        )
    )


def _reconcile(args: argparse.Namespace) -> int:
    from .ops import reconcile_report

    return _emit(asyncio.run(reconcile_report(default_paths(), account=args.account)))


def _import_posted(args: argparse.Namespace) -> int:
    from .ops import import_report

    return _emit(asyncio.run(import_report(default_paths(), args.source, account=args.account)))


def _orbit_tool(_args: argparse.Namespace) -> int:
    from .orbit_tool import main as orbit_tool_main

    return orbit_tool_main()


def _serve(args: argparse.Namespace) -> int:
    from .mcp import Runtime, build_server

    try:
        rt = Runtime()
    except PulsarError as exc:
        return _fail(exc)
    roots = rt.settings.media_roots
    # stderr: stdout is the MCP stream under --transport stdio.
    print(
        "pulsar: media path uploads "
        + (f"confined to {', '.join(map(str, roots))}" if roots else "off (no [media] roots)"),
        file=sys.stderr,
    )
    server = build_server(rt)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pulsar", description="X write connector for the constellation"
    )
    parser.add_argument("--version", action="version", version=f"pulsar {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", help="manage the account authorizations on this host")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    p_login = auth_sub.add_parser(
        "login", help="bind an account: OAuth 2.0 PKCE in a browser, then verify the handle"
    )
    p_login.add_argument(
        "--account",
        help="the account to bind, provider:handle (e.g. x:constworks); default: default_account",
    )
    p_login.add_argument(
        "--client-id", help="X app OAuth 2.0 client id (remembered after first use)"
    )
    p_login.add_argument(
        "--no-browser", action="store_true", help="print the URL instead of opening a browser"
    )
    p_login.set_defaults(func=_auth_login)
    p_status = auth_sub.add_parser("status", help="show each bound account and its token health")
    p_status.add_argument("--account", help="report only this account")
    status_mode = p_status.add_mutually_exclusive_group()
    status_mode.add_argument(
        "--offline", action="store_true", help="do not call X; report stored state only"
    )
    status_mode.add_argument(
        "--live",
        action="store_true",
        help="prove each binding: force a token refresh (rotates the pair), then GET /users/me",
    )
    p_status.set_defaults(func=_auth_status)
    p_logout = auth_sub.add_parser(
        "logout", help="delete an account's token bundle (the account is kept as revoked)"
    )
    p_logout.add_argument("--account", help="the account to log out; default: the default one")
    p_logout.set_defaults(func=_auth_logout)
    p_migrate = auth_sub.add_parser(
        "migrate", help="move single-account (phase 1) credentials into the account layout"
    )
    p_migrate.add_argument("--account", required=True, help="the alias they belong to, x:<handle>")
    p_migrate.set_defaults(func=_auth_migrate)

    p_st = sub.add_parser(
        "status", help="budgets, today's posts, quiet hours and unresolved writes (offline)"
    )
    p_st.add_argument("--account", help="report only this account")
    p_st.set_defaults(func=_status)

    p_hist = sub.add_parser("history", help="the newest ledger rows (offline)")
    p_hist.add_argument("--account", help="only this account's rows")
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.set_defaults(func=_history)

    p_val = sub.add_parser(
        "validate", help="check a plan offline: per account, what would post, digest and cost"
    )
    p_val.add_argument("plan", type=Path, help="plan file (YAML)")
    p_val.add_argument("--account", help="one of the plan's accounts, or the one to bind it to")
    p_val.set_defaults(func=_validate)

    p_pub = sub.add_parser(
        "publish",
        help="publish a plan through the ledger and policy (validates only without --yes)",
    )
    p_pub.add_argument("plan", type=Path, help="plan file (YAML)")
    p_pub.add_argument("--account", help="one of the plan's accounts, or the one to bind it to")
    p_pub.add_argument("--idempotency-key", help="default: derived from the digest and account")
    p_pub.add_argument("--caller", help="audit label recorded in the ledger")
    p_pub.add_argument("--yes", action="store_true", help="actually publish (costs money)")
    p_pub.set_defaults(func=_publish)

    p_rec = sub.add_parser(
        "reconcile",
        help="settle unknown writes from the account's timeline (reads X only when needed)",
    )
    p_rec.add_argument("--account", help="default: the default account")
    p_rec.set_defaults(func=_reconcile)

    p_imp = sub.add_parser(
        "import-posted", help="import a retired routine's posted.jsonl into the ledger (idempotent)"
    )
    p_imp.add_argument("source", type=Path, help="path to posted.jsonl")
    p_imp.add_argument("--account", help="the account those posts were made as")
    p_imp.set_defaults(func=_import_posted)

    serve = sub.add_parser("serve", help="run the MCP server")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument(
        "--host", default=CALLBACK_HOST, help="bind address for --transport http (default loopback)"
    )
    serve.add_argument("--port", type=int, default=8977)
    serve.set_defaults(func=_serve)

    p_orbit = sub.add_parser(
        "orbit-tool",
        help="answer one Orbit plugin request (envelope on stdin, response on stdout)",
    )
    p_orbit.set_defaults(func=_orbit_tool)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
