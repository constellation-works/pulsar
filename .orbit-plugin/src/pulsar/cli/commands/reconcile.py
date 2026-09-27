"""``pulsar reconcile``: settle writes whose outcome is unknown."""

from __future__ import annotations

import argparse
import asyncio

from .. import toolkit


def register(commands: toolkit.Commands) -> None:
    p = commands.add(
        "reconcile",
        group="Publish",
        help="settle writes whose outcome is unknown from the account's timeline "
        "(reads X only when something is unresolved)",
        epilog="Exit 0 only when every row it looked at is settled.",
    )
    p.add_argument("--account", help=f"the account to reconcile; {toolkit.ACCOUNT_DEFAULT}")
    p.set_defaults(func=_reconcile)


def _reconcile(args: argparse.Namespace, ctx: toolkit.Context) -> int:
    return toolkit.emit(
        asyncio.run(ctx.app.reconcile(account=args.account)),
        ctx,
        toolkit.views.reconcile,
    )
