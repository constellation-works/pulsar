"""``pulsar approve``, ``pulsar approvals`` and ``pulsar revoke``: a human's yes
to a plan's exact content, so an agent may publish it.

``approve`` prints the whole plan (every post's full text, media, reply
target, cost and digest) to stderr and records the approval only after the
human types the first characters of the digest at a terminal. There is no
flag that skips the question, and a stdin that is not a terminal is refused
(``interactive_only``): an approval is worth something only because a person
read what it approves.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pulsar.app.approvals import DEFAULT_POST_TTL, DEFAULT_REPLY_TTL, MAX_TTL, parse_ttl
from pulsar.app.ops import HISTORY_LIMIT_DEFAULT
from pulsar.internal.errors import INTERACTIVE_ONLY, PulsarError

from .. import toolkit
from .history import parse_limit
from .publish import PLAN_ACCOUNT_HELP

# How much of the digest the human types back.
CONFIRM_CHARS = 8


def register(commands: toolkit.Commands) -> None:
    p = commands.add(
        "approve",
        group="Publish",
        help="approve a plan's exact content so an agent may publish it (a human, at a terminal)",
        epilog="Examples:\n"
        "  pulsar approve engagement/2026-09-27/reply-1.yaml\n"
        "  pulsar approve content/2026-10/launch/plan.yaml --ttl 3d\n"
        "An edit to the plan changes its digest and needs a new approval; a new "
        "not_before does not.",
    )
    p.add_argument("plan", type=Path, help="plan file (YAML)")
    p.add_argument("--account", help=PLAN_ACCOUNT_HELP)
    p.add_argument(
        "--workspace",
        type=Path,
        metavar="DIR",
        help="resolve the plan's media against DIR and keep them inside it, as the Orbit "
        "plugin does (the command the plugin prints passes it); default: the current "
        "directory and the configured media roots",
    )
    p.add_argument(
        "--ttl",
        help=f"how long the approval lasts: 90m, 72h, 7d (at most {MAX_TTL.days}d); default: "
        f"{int(DEFAULT_REPLY_TTL.total_seconds() // 3600)}h for a reply, "
        f"{DEFAULT_POST_TTL.days}d otherwise",
    )
    p.set_defaults(func=_approve)

    p_list = commands.add(
        "approvals", group="Observe", help="approvals, newest first, with their state (offline)"
    )
    p_list.add_argument(
        "--account", help=f"only this account's ({toolkit.ACCOUNT_HELP}); default: all"
    )
    p_list.add_argument(
        "--limit", type=parse_limit, default=HISTORY_LIMIT_DEFAULT, help="rows to show; default: 20"
    )
    p_list.set_defaults(func=_approvals)

    p_revoke = commands.add(
        "revoke",
        group="Publish",
        help="revoke an approval by id; a post already published under it stays",
    )
    p_revoke.add_argument("id", type=int, help="the approval's id (`pulsar approvals`)")
    p_revoke.set_defaults(func=_revoke)


def _approve(args: argparse.Namespace, ctx: toolkit.Context) -> int:
    ttl = parse_ttl(args.ttl) if args.ttl else None
    preview, _ = ctx.app.approve_preview(
        args.plan, account=args.account, ttl=ttl, workspace=args.workspace
    )
    sys.stderr.write(describe(preview))
    if not sys.stdin.isatty():
        raise PulsarError(
            INTERACTIVE_ONLY,
            "approving needs a human at a terminal: run `pulsar approve` in one and type the "
            "digest's first characters when asked",
        )
    accounts: list[Mapping[str, Any]] = preview["accounts"]
    digests = {str(a["account"]): str(a["digest"]) for a in accounts}
    for digest in dict.fromkeys(digests.values()):  # each distinct digest, in order
        whose = ", ".join(a for a, d in digests.items() if d == digest)
        sys.stderr.write(
            f"\nTo approve {whose}, type the first {CONFIRM_CHARS} characters of its digest "
            "after 'sha256:': "
        )
        sys.stderr.flush()
        typed = _hex(sys.stdin.readline().strip().lower())
        if len(typed) < CONFIRM_CHARS or not _hex(digest).startswith(typed):
            toolkit.notice("not approved; nothing was recorded")
            return toolkit.EXIT_FAILED
    return toolkit.emit(
        ctx.app.approve(
            args.plan, expect=digests, account=args.account, ttl=ttl, workspace=args.workspace
        ),
        ctx,
        toolkit.views.approvals,
    )


def _hex(digest: str) -> str:
    """A digest without its ``sha256:`` label: the part a human types back."""
    return digest.removeprefix("sha256:")


def describe(preview: Mapping[str, Any]) -> str:
    """The whole plan as a person reads it before approving: nothing truncated."""
    lines = [
        f"Approving {preview['source']} for publishing by an agent",
        f"  expires  {preview['expires_at']} (unless used or revoked first)",
    ]
    for account in preview["accounts"]:
        posts: list[Mapping[str, Any]] = account["posts"]
        lines += [
            "",
            f"{account['account']}: {len(posts)} post(s), "
            f"about ${account['estimated_cost_usd']} (from the configured prices)",
            f"  digest   {account['digest']}",
        ]
        if account.get("key"):
            lines.append(f"  key      {account['key']} (published once under this key)")
        if account.get("reply_to"):
            lines.append(f"  replies to post {account['reply_to']}")
        if account.get("quote"):
            lines.append(f"  quotes post {account['quote']}")
        for n, post in enumerate(posts, 1):
            lines.append(f"  [{n}] ({post['length']}/{post['max_length']})")
            lines += [f"      {line}" for line in str(post["text"]).splitlines() or [""]]
            media: list[Mapping[str, Any]] = post.get("media") or []
            for m in media:
                lines.append(f"      + {m['mime']}, {m['bytes']} bytes, alt: {m['alt']!r}")
        earlier_approvals: list[Mapping[str, Any]] = account.get("approvals") or []
        for earlier in earlier_approvals:
            lines.append(
                f"  earlier approval #{earlier['id']}: {earlier['state']} "
                f"(by {earlier['approved_by']}, {earlier['created_at']})"
            )
    return "\n".join(lines) + "\n"


def _approvals(args: argparse.Namespace, ctx: toolkit.Context) -> int:
    out, code = ctx.app.approvals(account=args.account, limit=args.limit)
    if not out["approvals"]:
        toolkit.notice("no approvals" + (f" for {args.account}" if args.account else ""))
    return toolkit.emit((out, code), ctx, toolkit.views.approvals)


def _revoke(args: argparse.Namespace, ctx: toolkit.Context) -> int:
    return toolkit.emit(ctx.app.revoke(args.id), ctx)
