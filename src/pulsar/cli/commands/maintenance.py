"""``pulsar import-posted`` and ``pulsar migrate``: one-off upkeep of the home."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from . import views
from .context import Context, emit
from .parser import ACCOUNT_DEFAULT, Commands


def register(commands: Commands) -> None:
    p_imp = commands.add(
        "import-posted",
        group="Maintenance",
        help="import a retired routine's posted.jsonl into the ledger (idempotent); "
        "reports only without --confirm",
    )
    p_imp.add_argument("source", type=Path, help="path to posted.jsonl")
    p_imp.add_argument("--account", help=f"the account those posts were made as; {ACCOUNT_DEFAULT}")
    p_imp.add_argument("--confirm", action="store_true", help="actually write the rows")
    p_imp.set_defaults(func=_import_posted)

    p_mig = commands.add(
        "migrate",
        group="Maintenance",
        help="upgrade the home in place: the ledger schema, then phase 1 credentials; "
        "reports only without --confirm",
        epilog="An upgraded ledger is refused by older pulsar versions; moved credentials "
        "stay moved.",
    )
    p_mig.add_argument("--confirm", action="store_true", help="actually upgrade the home")
    p_mig.set_defaults(func=_migrate)


def _import_posted(args: argparse.Namespace, ctx: Context) -> int:
    return emit(
        asyncio.run(ctx.app.import_posted(args.source, account=args.account, confirm=args.confirm)),
        ctx,
        views.import_posted,
    )


def _migrate(args: argparse.Namespace, ctx: Context) -> int:
    return emit(ctx.app.migrate(confirm=args.confirm), ctx)
