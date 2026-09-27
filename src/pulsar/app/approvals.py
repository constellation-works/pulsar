"""The verbs behind ``pulsar approve | approvals | revoke``: a human's yes to a
plan's exact content, recorded in the ledger.

Approving is a human act, so these are CLI verbs only: no MCP tool and no
Orbit plugin tool records or revokes an approval. ``preview`` shows what
would be approved (per account: the posts, the digest, the cost, earlier
approvals of the same digest) and writes nothing; ``approve`` re-reads the
plan and records the approval only if every digest is still the one shown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pulsar.app.core.publishing import Plan, Prepared
from pulsar.internal.errors import INVALID_ARGUMENT, PulsarError

from .interfaces import Report, Runtime
from .ops import check_limit, read_plan

# A reply answers a conversation that moves on; a post can wait for its slot.
DEFAULT_REPLY_TTL = timedelta(hours=72)
DEFAULT_POST_TTL = timedelta(days=7)
MAX_TTL = timedelta(days=30)

_TTL = re.compile(r"^(\d{1,5})([mhd])$")
_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_ttl(text: str) -> timedelta:
    """``90m``, ``72h`` or ``7d``: at least a minute, at most ``MAX_TTL``."""
    match = _TTL.fullmatch(text.strip())
    ttl = int(match[1]) * _UNITS[match[2]] if match else None
    if ttl is None or not timedelta(minutes=1) <= ttl <= MAX_TTL:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"ttl must look like 90m, 72h or 7d, from 1m to {MAX_TTL.days}d; got {text!r}",
            detail={"ttl": text},
        )
    return ttl


def default_ttl(plan: Plan) -> timedelta:
    return DEFAULT_REPLY_TTL if plan.reply_to is not None else DEFAULT_POST_TTL


def _prepared(rt: Runtime, plan_path: Path, account: str | None) -> tuple[Plan, list[Prepared]]:
    plan, targets = rt.plan_targets(read_plan(plan_path), account)
    return plan, [rt.publisher.prepare(plan, rt.offline_bound(alias)) for alias in targets]


def preview(rt: Runtime, plan_path: Path, *, account: str | None, ttl: timedelta | None) -> Report:
    """What approving ``plan_path`` would approve, per account. Writes nothing."""
    plan, prepared = _prepared(rt, plan_path, account)
    ttl = ttl or default_ttl(plan)
    now = rt.ledger.now()
    accounts = [
        {
            **p.report(),
            "approvals": [a.to_dict(now) for a in rt.ledger.approvals_for(p.bound.alias, p.digest)],
        }
        for p in prepared
    ]
    return {
        "approved": False,
        "source": str(plan_path),
        "ttl_s": int(ttl.total_seconds()),
        "expires_at": (datetime.now(UTC) + ttl).isoformat(timespec="seconds"),
        "accounts": accounts,
    }, 0


def approve(
    rt: Runtime,
    plan_path: Path,
    *,
    account: str | None,
    ttl: timedelta | None,
    approved_by: str,
    expect: Mapping[str, str],
) -> Report:
    """Record an approval of ``plan_path`` for each of its accounts, if each
    digest is the one in ``expect`` (what the human was shown)."""
    plan, prepared = _prepared(rt, plan_path, account)
    changed = [p.bound.alias for p in prepared if expect.get(p.bound.alias) != p.digest]
    if changed or len(expect) != len(prepared):
        raise PulsarError(
            INVALID_ARGUMENT,
            f"{plan_path} changed since it was shown; nothing was approved — run it again",
            detail={"accounts": changed},
        )
    ttl = ttl or default_ttl(plan)
    expires_at = datetime.now(UTC) + ttl
    now = rt.ledger.now()
    recorded: list[dict[str, Any]] = []
    for p in prepared:
        approval = rt.ledger.approve(
            account_alias=p.bound.alias,
            digest=p.digest,
            approved_by=approved_by,
            source=str(plan_path),
            posts=len(p.posts),
            est_cost_usd=p.estimated_cost_usd,
            expires_at=expires_at,
        )
        recorded.append(approval.to_dict(now))
    return {"approved": True, "source": str(plan_path), "approvals": recorded}, 0


def listing(rt: Runtime, *, account: str | None, limit: int) -> Report:
    """The newest approvals, each with its state now."""
    limit = check_limit(limit)
    alias = rt.account(account).alias if account is not None else None
    now = rt.ledger.now()
    rows = rt.ledger.approvals(account_alias=alias, limit=limit)
    return {"approvals": [a.to_dict(now) for a in rows]}, 0


def revoke(rt: Runtime, approval_id: int) -> Report:
    """Revoke an approval; a write already published under it stays published."""
    approval = rt.ledger.revoke_approval(approval_id)
    return {"revoked": approval.to_dict(rt.ledger.now())}, 0
