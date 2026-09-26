"""``pulsar validate`` and ``pulsar publish``: a plan checked offline, then posted."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pulsar.app import CLI_CALLER, publish_report, validate_report

from .. import views
from ..context import Context, emit, notice
from ..parser import ACCOUNT_DEFAULT, Commands

PLAN_ACCOUNT_HELP = (
    "one of the plan's accounts, or the one to bind a plan that names none; " + ACCOUNT_DEFAULT
)


def register(commands: Commands) -> None:
    p_val = commands.add(
        "validate",
        group="Publish",
        help="check a plan offline: per account, what would post, its digest and cost",
    )
    p_val.add_argument("plan", type=Path, help="plan file (YAML)")
    p_val.add_argument("--account", help=PLAN_ACCOUNT_HELP)
    p_val.set_defaults(func=_validate)

    p_pub = commands.add(
        "publish",
        group="Publish",
        help="publish a plan through the ledger and policy; validates only without --confirm",
        epilog="Examples:\n"
        "  pulsar publish plan.yaml            # what would post, and its cost\n"
        "  pulsar publish plan.yaml --confirm  # post it (re-running replays the receipt)",
    )
    p_pub.add_argument("plan", type=Path, help="plan file (YAML)")
    p_pub.add_argument("--account", help=PLAN_ACCOUNT_HELP)
    p_pub.add_argument(
        "--idempotency-key",
        help="the write's key: re-running with it replays the receipt instead of posting again; "
        "default: derived from the plan's digest and the account",
    )
    p_pub.add_argument(
        "--caller",
        help=f"audit label recorded in the ledger; default: $PULSAR_CALLER, else {CLI_CALLER}",
    )
    p_pub.add_argument("--confirm", action="store_true", help="actually publish (costs money)")
    p_pub.add_argument("--yes", action="store_true", help="deprecated alias of --confirm")
    p_pub.set_defaults(func=_publish)


def _validate(args: argparse.Namespace, ctx: Context) -> int:
    return emit(
        asyncio.run(validate_report(ctx.paths, args.plan, account=args.account)),
        ctx,
        views.plan_report,
    )


def _publish(args: argparse.Namespace, ctx: Context) -> int:
    if args.yes:
        notice("--yes is deprecated; use --confirm")
    return emit(
        asyncio.run(
            publish_report(
                ctx.paths,
                args.plan,
                account=args.account,
                idempotency_key=args.idempotency_key,
                caller=args.caller,
                confirm=args.confirm or args.yes,
                transport=ctx.transport,
            )
        ),
        ctx,
        views.publish,
    )
