"""``pulsar status``: budgets, today's posts, quiet hours and unresolved writes."""

from __future__ import annotations

import argparse

from pulsar.cli.toolkit import ACCOUNT_HELP, Commands, Context, emit, notice, views


def register(commands: Commands) -> None:
    p = commands.add(
        "status",
        help="budgets, today's posts, quiet hours and unresolved writes (offline)",
        group="Observe",
    )
    p.add_argument("--account", help=f"report only this account ({ACCOUNT_HELP}); default: all")
    p.set_defaults(func=_status)


def _status(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ctx.app.status(account=args.account)
    if not out["accounts"]:
        notice("no account is bound; nothing to report")
    return emit((out, code), ctx, views.status)
