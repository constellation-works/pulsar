"""``pulsar auth login | status | logout | migrate``: the human side of accounts."""

from __future__ import annotations

import argparse
import asyncio

from .. import views
from ..context import Context, emit, notice
from ..errors import UsageError
from ..parser import ACCOUNT_DEFAULT, ACCOUNT_HELP, Commands


def register(commands: Commands) -> None:
    auth = commands.add(
        "auth",
        help="bind, check and log out the accounts on this host",
        group="Accounts",
        epilog="Examples:\n  pulsar auth login --account x:<handle> --no-browser\n"
        "  pulsar auth status --live",
    )
    auth.usage = "pulsar auth [OPTIONS] COMMAND ..."
    auth.set_defaults(func=None, help_parser=auth)
    sub = Commands(auth, commands.common, dest="auth_command")

    p_login = sub.add(
        "login",
        group="Commands",
        help="bind an account: OAuth 2.0 PKCE in a browser, then verify the handle with X",
        epilog="Examples:\n  pulsar auth login --account x:<handle> --client-id <CLIENT_ID>\n"
        "  pulsar auth login --account x:<handle> --no-browser   # over SSH",
    )
    p_login.add_argument(
        "--account", help=f"the account to bind ({ACCOUNT_HELP}); {ACCOUNT_DEFAULT}"
    )
    p_login.add_argument(
        "--client-id",
        help="the X app's OAuth 2.0 client id; default: the one remembered from the last login",
    )
    p_login.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL on stderr instead of opening a browser",
    )
    p_login.set_defaults(func=_login)

    p_status = sub.add(
        "status",
        group="Commands",
        help="each bound account and its health, from local state (no network)",
        epilog="Exit 0 only when every reported account is healthy.",
    )
    p_status.add_argument(
        "--account", help=f"report only this account ({ACCOUNT_HELP}); default: all"
    )
    status_mode = p_status.add_mutually_exclusive_group()
    status_mode.add_argument(
        "--live",
        action="store_true",
        help="prove each binding: force a token refresh (rotates the pair), then GET /users/me",
    )
    status_mode.add_argument(
        "--offline", action="store_true", help="deprecated, no effect: the default is offline"
    )
    p_status.set_defaults(func=_status)

    p_logout = sub.add(
        "logout",
        group="Commands",
        help="delete an account's tokens (the account is kept as revoked); needs --confirm",
    )
    p_logout.add_argument("--account", help=f"the account to log out; {ACCOUNT_DEFAULT}")
    p_logout.add_argument("--confirm", action="store_true", help="actually delete the tokens")
    p_logout.set_defaults(func=_logout)

    p_migrate = sub.add(
        "migrate",
        group="Commands",
        help="move single-account (phase 1) credentials into the account layout; "
        "reports only without --confirm",
    )
    p_migrate.add_argument(
        "--account",
        help="the alias they belong to, x:<handle>; default: default_account, else the "
        "handle whoami.json recorded",
    )
    p_migrate.add_argument("--confirm", action="store_true", help="actually move them")
    p_migrate.set_defaults(func=_migrate)


def _migrate_quietly(ctx: Context) -> None:
    """First-use migration of a phase 1 home before a login or logout."""
    message = ctx.app.migrate_legacy_quietly()
    if message:
        notice(message)


def _login(args: argparse.Namespace, ctx: Context) -> int:
    alias = args.account or ctx.app.default_account()
    if not alias:
        raise UsageError("--account is required: the account to bind, e.g. --account x:<handle>")
    client_id = args.client_id or ctx.app.remembered_client_id()
    if not client_id:
        raise UsageError("--client-id is required the first time: the X app's OAuth 2.0 client id")
    _migrate_quietly(ctx)
    return emit(ctx.app.login(alias, client_id, open_browser=not args.no_browser), ctx)


def _status(args: argparse.Namespace, ctx: Context) -> int:
    if args.offline:
        notice("--offline is deprecated and has no effect: the default makes no network call")
    out, code = asyncio.run(ctx.app.auth_status(account=args.account, live=args.live))
    if not out["accounts"]:
        notice("no account is bound; a human runs `pulsar auth login --account x:<handle>`")
    if out["legacy"] is not None and out["legacy"]["message"]:
        notice(out["legacy"]["message"])
    for entry in out["accounts"]:
        remedy = ctx.app.attention(entry)
        if remedy is not None:
            notice(remedy)
    return emit((out, code), ctx, views.auth_status)


def _logout(args: argparse.Namespace, ctx: Context) -> int:
    # Refuse before touching anything: not even the first-use migration runs
    # without --confirm (STD-01 §R5).
    if not args.confirm:
        raise UsageError(
            f"logging out {args.account or 'the default account'} deletes its tokens and "
            "needs a human to log in again; pass --confirm to proceed",
            detail={"account": args.account},
        )
    _migrate_quietly(ctx)
    return emit(ctx.app.logout(args.account), ctx)


def _migrate(args: argparse.Namespace, ctx: Context) -> int:
    """Report what the move would do; ``--confirm`` makes it (STD-01 §R5)."""
    return emit(ctx.app.auth_migrate(account=args.account, confirm=args.confirm), ctx)
