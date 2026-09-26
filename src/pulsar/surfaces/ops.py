"""The operator verbs behind ``pulsar status | history | validate | publish |
reconcile | import-posted``.

Each returns ``(report, exit_code)`` so tests drive them without a shell.
Only ``publish --yes``, ``reconcile`` (when something is unresolved) and
``import-posted`` (for an account whose identity is not cached yet) reach
the network; everything else reads the ledger, the registry and the plan.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..core.accounts import require_expected
from ..core.errors import INVALID_ARGUMENT, UNSUPPORTED, PulsarError
from ..core.importer import import_posted
from ..core.ledger import PUBLISHED, SKIPPED, AccountRef
from ..core.paths import Paths
from ..core.plan import Plan, alias_provider
from ..core.policy import Policy, day_window, month_window
from ..core.publisher import STALE_SUBMITTING
from ..core.writelog import resolve_caller
from ..providers.x.adapter import post_url
from .mcp import Runtime

CLI_CALLER = "pulsar-cli"

Report = tuple[dict[str, Any], int]


def _error(exc: PulsarError, **extra: Any) -> Report:
    return {**extra, **exc.to_result()}, 1


def _read_plan(path: Path) -> Plan:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PulsarError(INVALID_ARGUMENT, f"cannot read plan {path}: {exc.strerror}") from exc
    return Plan.from_yaml(text)


def budget_report(
    paths: Paths, *, account: str | None = None, now: datetime | None = None
) -> Report:
    """Budgets, today's posts, quiet hours and unresolved writes per account. Offline."""
    rt = Runtime(paths)
    now = now or datetime.now(UTC)
    try:
        aliases = (
            [rt.account(account).alias] if account is not None else sorted(rt.registry.accounts())
        )
    except PulsarError as exc:
        return _error(exc)
    policy = Policy(rt.settings.policy)
    day_start, _ = day_window(now, rt.settings.policy.tz)
    month_start, _ = month_window(now, rt.settings.policy.tz)
    unresolved = rt.ledger.unresolved(stale_after=STALE_SUBMITTING, now=now)
    entries: list[dict[str, Any]] = []
    for alias in aliases:
        usage = rt.ledger.usage(alias, day_start=day_start, month_start=month_start)
        entries.append(
            {
                "alias": alias,
                **policy.status(usage=usage, now=now),
                "unresolved": [r.key for r in unresolved if r.account_alias == alias],
            }
        )
    out = {
        "home": str(paths.home),
        "default_account": rt.settings.default_account,
        "note": "day and month spend is across every account; posts are per account",
        "accounts": entries,
    }
    return out, 0


def history_report(paths: Paths, *, account: str | None = None, limit: int = 20) -> Report:
    rt = Runtime(paths)
    try:
        alias = rt.account(account).alias if account is not None else None
    except PulsarError as exc:
        return _error(exc)
    rows = rt.ledger.history(limit=limit, account_alias=alias)
    return {"writes": [r.to_dict() for r in rows]}, 0


async def validate_report(paths: Paths, plan_path: Path, *, account: str | None = None) -> Report:
    """What ``publish`` would send, per account, with digests and cost. Offline."""
    rt = Runtime(paths)
    try:
        plan, targets = rt.plan_targets(_read_plan(plan_path), account)
        reports = [rt.publisher.prepare(plan, rt.offline_bound(a)).report() for a in targets]
    except PulsarError as exc:
        return _error(exc)
    finally:
        await rt.aclose()
    return {"ok": True, "accounts": reports}, 0


async def publish_report(
    paths: Paths,
    plan_path: Path,
    *,
    account: str | None = None,
    idempotency_key: str | None = None,
    caller: str | None = None,
    yes: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Report:
    """Publish a plan to each of its accounts. Without ``yes`` it only validates."""
    if not yes:
        out, code = await validate_report(paths, plan_path, account=account)
        if code == 0:
            out["published"] = False
            out["note"] = "validated only; re-run with --yes to publish (this costs money)"
        return out, code
    rt = Runtime(paths, transport=transport)
    results: list[dict[str, Any]] = []
    code = 0
    try:
        plan, targets = rt.plan_targets(_read_plan(plan_path), account)
        if idempotency_key is not None and len(targets) > 1:
            raise PulsarError(
                INVALID_ARGUMENT,
                "an idempotency key names one account's write; pick one with --account",
                detail={"accounts": targets},
            )
        for alias in targets:
            try:
                bound = await rt.bound(alias)
                prepared = rt.publisher.prepare(plan, bound)
                with rt.watch_expiry(alias):
                    outcome = await rt.publisher.publish(
                        prepared,
                        idempotency_key=idempotency_key,
                        caller=resolve_caller(caller or CLI_CALLER),
                    )
            except PulsarError as exc:
                results.append({"account": alias, **exc.to_result()})
                code = 1
                continue
            entry: dict[str, Any] = {"ok": outcome.error is None, **outcome.receipt()}
            if outcome.error is not None:
                entry["error"] = outcome.error.to_result()
                code = 1
            elif outcome.record.state not in (PUBLISHED, SKIPPED):
                code = 1
            results.append(entry)
    except PulsarError as exc:
        return _error(exc, results=results)
    finally:
        await rt.aclose()
    return {"results": results}, code


async def reconcile_report(
    paths: Paths,
    *,
    account: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    now: datetime | None = None,
) -> Report:
    """Settle an account's unknown writes from its timeline.

    Calls the provider only when the account has something to settle; the
    timeline read is billed per post returned.
    """
    rt = Runtime(paths, transport=transport)
    try:
        alias = rt.account(account).alias
        pending = [
            r.key
            for r in rt.ledger.unresolved(
                stale_after=STALE_SUBMITTING, now=now or datetime.now(UTC)
            )
            if r.account_alias == alias
        ]
        if not pending:
            return {"account": alias, "results": []}, 0
        bound = await rt.bound(alias)
        with rt.watch_expiry(alias):
            results = await rt.publisher.reconcile(bound)
    except PulsarError as exc:
        return _error(exc)
    finally:
        await rt.aclose()
    settled = all(r["state"] not in ("unknown", "submitting") for r in results)
    return {"account": alias, "results": results}, 0 if settled else 1


async def import_report(
    paths: Paths,
    source: Path,
    *,
    account: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Report:
    """Import a retired routine's ``posted.jsonl`` as the account's rows. Idempotent."""
    rt = Runtime(paths, transport=transport)
    try:
        found = await rt.identity(account)
        require_expected(found, rt.settings)
        if alias_provider(found.alias) != "x":
            raise PulsarError(UNSUPPORTED, "posted.jsonl import is for X accounts")
        handle = found.handle or ""
        ref = AccountRef(
            alias=found.alias,
            provider="x",
            user_id=found.provider_user_id or "",
            handle=handle,
        )
        report = import_posted(
            rt.ledger, source, account=ref, url_for=lambda post_id: post_url(handle, post_id)
        )
    except PulsarError as exc:
        return _error(exc)
    except OSError as exc:
        return _error(PulsarError(INVALID_ARGUMENT, f"cannot read {source}: {exc.strerror}"))
    finally:
        await rt.aclose()
    out = {"account": ref.alias, "source": str(source), **report.to_dict()}
    return out, 0 if not report.conflicts and not report.errors else 1
