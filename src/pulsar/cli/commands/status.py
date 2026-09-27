"""``pulsar status``: budgets, today's posts, quiet hours and unresolved writes."""

from __future__ import annotations

import argparse

from .. import toolkit


def register(commands: toolkit.Commands) -> None:
    p = commands.add(
        "status",
        help="budgets, today's posts, quiet hours and unresolved writes (offline)",
        group="Observe",
    )
    p.add_argument(
        "--account", help=f"report only this account ({toolkit.ACCOUNT_HELP}); default: all"
    )
    p.set_defaults(func=_status)


def _status(args: argparse.Namespace, ctx: toolkit.Context) -> int:
    out, code = ctx.app.status(account=args.account)
    if not out["accounts"]:
        toolkit.notice("no account is bound; nothing to report")
    return toolkit.emit((out, code), ctx, toolkit.views.status)
