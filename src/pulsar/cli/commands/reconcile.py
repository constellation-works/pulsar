"""``pulsar reconcile``: settle writes whose outcome is unknown."""

from __future__ import annotations

import argparse
import asyncio

from pulsar.app import reconcile_report

from .. import views
from ..context import Context, emit
from ..parser import ACCOUNT_DEFAULT, Commands


def register(commands: Commands) -> None:
    p = commands.add(
        "reconcile",
        group="Publish",
        help="settle writes whose outcome is unknown from the account's timeline "
        "(reads X only when something is unresolved)",
        epilog="Exit 0 only when every row it looked at is settled.",
    )
    p.add_argument("--account", help=f"the account to reconcile; {ACCOUNT_DEFAULT}")
    p.set_defaults(func=_reconcile)


def _reconcile(args: argparse.Namespace, ctx: Context) -> int:
    return emit(
        asyncio.run(reconcile_report(ctx.paths, account=args.account, transport=ctx.transport)),
        ctx,
        views.reconcile,
    )
