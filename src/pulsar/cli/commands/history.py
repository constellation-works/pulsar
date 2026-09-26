"""``pulsar history``: the newest ledger rows."""

from __future__ import annotations

import argparse

from pulsar.app import ops

from .. import views
from ..context import Context, emit, notice
from ..parser import ACCOUNT_HELP, Commands


def register(commands: Commands) -> None:
    p = commands.add("history", help="the newest ledger rows (offline)", group="Observe")
    p.add_argument("--account", help=f"only this account's rows ({ACCOUNT_HELP}); default: all")
    p.add_argument(
        "--limit",
        type=_limit,
        default=ops.HISTORY_LIMIT_DEFAULT,
        help=f"rows to show, 1..{ops.HISTORY_LIMIT_MAX}; default: {ops.HISTORY_LIMIT_DEFAULT}",
    )
    p.set_defaults(func=_history)


def _limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {value!r}") from None
    if not 1 <= limit <= ops.HISTORY_LIMIT_MAX:
        raise argparse.ArgumentTypeError(f"must be 1..{ops.HISTORY_LIMIT_MAX}, got {limit}")
    return limit


def _history(args: argparse.Namespace, ctx: Context) -> int:
    out, code = ops.history_report(ctx.paths, account=args.account, limit=args.limit)
    if not out["writes"]:
        notice("no ledger rows" + (f" for {args.account}" if args.account else ""))
    elif out["truncated"]:
        notice(f"showing {len(out['writes'])} of {out['total']} rows; raise --limit for more")
    return emit((out, code), ctx, views.history)
