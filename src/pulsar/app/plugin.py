"""The Orbit plugin's tools: ``pulsar.status``, ``pulsar.validate`` and
``pulsar.history``. All three are offline and read-only.

Each takes the ``Runtime`` it runs against and returns the tool's output, or
raises ``PulsarError``; the Orbit backend (``pulsar.orbit``) owns the
envelope, the input checks and the sandbox's rules.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pulsar.app.core.account import home_command
from pulsar.app.core.ledger import PUBLISHED, PlanRecord
from pulsar.app.core.publishing import Plan, open_beneath
from pulsar.internal.errors import (
    INVALID_ARGUMENT,
    INVALID_MEDIA,
    INVALID_PLAN,
    INVALID_TEXT,
    SECRET_DETECTED,
    UNSUPPORTED,
    PulsarError,
)

from .health import attention as health_attention
from .health import auth_report
from .interfaces import Runtime
from .ops import budget_report, check_limit

# A plan source is small YAML; refuse anything that is plainly not one.
SOURCE_MAX_BYTES = 256 * 1024

# What is wrong with the plan itself, which ``validate`` answers as ``valid: false``.
PLAN_VERDICTS = frozenset({INVALID_PLAN, INVALID_TEXT, INVALID_MEDIA, SECRET_DETECTED, UNSUPPORTED})

Output = dict[str, Any]


async def status(rt: Runtime, *, account: str | None) -> Output:
    """Accounts, token health, budget use, unresolved writes, last publication.

    Token health comes from local state (``unverified`` when that cannot
    settle it); nothing is migrated or written.
    """
    home = rt.paths.home
    auth, _ = await auth_report(rt, account=account)
    budget, _ = budget_report(rt, account=account)
    usage = {entry["alias"]: entry for entry in budget["accounts"]}
    accounts: list[dict[str, Any]] = []
    attention: list[str] = []
    for entry in auth["accounts"]:
        alias = entry["alias"]
        spent = usage.get(alias, {})
        row = {
            "alias": alias,
            "status": entry["status"],
            "expected_handle": entry["expected_handle"],
            "authorized": entry["authorized"],
            "reauth_required": entry["reauth_required"],
            "token_state": entry["token_state"],
            "access_token_expires_in_s": entry["access_token_expires_in_s"],
            "health": entry["health"],
            "reason": entry["reason"],
            "healthy": entry["healthy"],
            "posts": spent.get("posts"),
            "day": spent.get("day"),
            "month": spent.get("month"),
            "quiet": spent.get("quiet"),
            "unresolved": spent.get("unresolved", []),
            "last_published": _last_published(rt, alias),
        }
        accounts.append(row)
        if (note := health_attention(entry, home)) is not None:
            attention.append(note)
        if row["unresolved"]:
            attention.append(
                f"{alias}: {len(row['unresolved'])} write(s) with an unknown outcome "
                f"(`{home_command(home, f'reconcile --account {alias}')}`)"
            )
    if not accounts:
        # No home here: this is a conformance golden and the sandbox's home varies.
        attention.append("no account is bound (`pulsar auth login --account x:<handle>`)")
    if auth["legacy"] is not None:
        remedy = auth["legacy"]["message"] or f"run `{home_command(home, 'migrate --confirm')}`"
        attention.append(f"legacy credentials: {remedy}")
    return {
        "default_account": auth["default_account"],
        "healthy": not attention,
        "attention": attention,
        "accounts": accounts,
    }


def _last_published(rt: Runtime, alias: str) -> dict[str, Any] | None:
    record = rt.ledger.last_published(alias)
    if record is None:
        return None
    return {"key": record.key, "url": record.url, "at": record.updated_at}


def validate(
    rt: Runtime,
    *,
    plan: Mapping[str, Any] | None = None,
    source_text: str | None = None,
    account: str | None,
) -> Output:
    """What publishing a plan (a mapping, or a source's YAML) would send, per
    account. Claims and sends nothing.

    A plan that fails validation is a result (``valid: false``), not a tool
    error: an agent drafting a post needs the code and detail to fix it.
    """
    try:
        if source_text is not None:
            parsed = Plan.from_yaml(source_text)
        else:
            parsed = Plan.from_mapping(plan or {})
        parsed, targets = rt.plan_targets(parsed, account)
        reports = [rt.publisher.prepare(parsed, rt.offline_bound(a)).report() for a in targets]
    except PulsarError as exc:
        if exc.code not in PLAN_VERDICTS:
            raise  # the account, the home or the storage: a tool error, not a verdict
        return {"valid": False, "error": exc.to_envelope()["error"]}
    return {"valid": True, "accounts": reports}


def read_source(workspace: Path, source: str) -> str:
    """The plan file ``source`` names inside ``workspace``, read without following
    a symlink out of the workspace between the check and the open. An unreadable
    source is ``invalid_argument``; what it says is the plan's business."""
    detail = {"source": source}
    try:
        path = (workspace / source).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PulsarError(
            INVALID_ARGUMENT, f"cannot read {source}: {_reason(exc)}", detail=detail
        ) from exc
    if not path.is_relative_to(workspace) or path == workspace:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"`source` must be a file inside the workspace ({workspace})",
            detail=detail,
        )
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise PulsarError(INVALID_ARGUMENT, f"{source} is not a regular file", detail=detail)
        fd = open_beneath(path, workspace)
    except OSError as exc:
        raise PulsarError(
            INVALID_ARGUMENT, f"cannot read {source}: {_reason(exc)}", detail=detail
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            raise PulsarError(
                INVALID_ARGUMENT, f"{source} changed while it was being opened", detail=detail
            )
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(SOURCE_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > SOURCE_MAX_BYTES:
        raise PulsarError(
            INVALID_ARGUMENT, f"{source} is over {SOURCE_MAX_BYTES} bytes", detail=detail
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PulsarError(INVALID_ARGUMENT, f"{source} is not UTF-8", detail=detail) from exc


def _reason(exc: BaseException) -> str:
    return exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__


def history(rt: Runtime, *, account: str | None, limit: int) -> Output:
    """The newest ledger rows, flattened for a table, and how many there are."""
    limit = check_limit(limit)
    alias = rt.account(account).alias if account is not None else None
    rows = [_row(r) for r in rt.ledger.history(limit=limit, account_alias=alias)]
    total = rt.ledger.count(account_alias=alias)
    return {"rows": rows, "total": total, "truncated": total > len(rows)}


def _row(record: PlanRecord) -> dict[str, Any]:
    return {
        "key": record.key,
        "tool": record.tool,
        "account": record.account_alias,
        "state": record.state,
        "posts": len(record.items),
        "published": sum(1 for i in record.items if i.state == PUBLISHED),
        "url": record.url,
        "cost_usd": round(sum(i.est_cost_usd for i in record.items), 6),
        "error_code": record.error_code,
        "caller": record.caller,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
