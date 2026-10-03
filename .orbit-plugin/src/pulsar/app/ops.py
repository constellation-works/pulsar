"""The operator verbs behind ``pulsar status | history | validate | publish |
reconcile | import-posted | migrate``.

Each takes the ``Runtime`` it runs against (``App`` builds it: read-only for
the reports and the previews) and returns ``(report, exit_code)`` so tests
drive them without a shell, and
raises ``PulsarError`` when the command could not run at all; the CLI prints
the report on stdout and the error on stderr. Only ``publish --confirm``,
``reconcile`` (when something is unresolved) and ``import-posted --confirm``
(for an account whose identity is not cached yet) reach the network. The
reports (``status``, ``history``, ``validate``) read the home without
changing it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pulsar.app.core.account import MigrationResult, alias_provider, require_expected
from pulsar.app.core.channels.x import post_url
from pulsar.app.core.ledger import PUBLISHED, SKIPPED, AccountRef, State, check_key, is_settled
from pulsar.app.core.publishing import (
    STALE_SUBMITTING,
    Outcome,
    Plan,
    Policy,
    Prepared,
    day_window,
    month_window,
)
from pulsar.internal.errors import INVALID_ARGUMENT, UNSUPPORTED, PulsarError

from .importer import import_posted
from .interfaces import Report, Runtime

CLI_CALLER = "pulsar-cli"
HISTORY_LIMIT_DEFAULT = 20
HISTORY_LIMIT_MAX = 100


def read_plan(path: Path) -> Plan:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PulsarError(
            INVALID_ARGUMENT, f"cannot read plan {path}: {exc.strerror}", detail={"plan": str(path)}
        ) from exc
    return Plan.from_yaml(text)


def check_limit(limit: int) -> int:
    """``limit`` as given, or ``invalid_argument``: never clamped."""
    if isinstance(limit, bool) or not 1 <= limit <= HISTORY_LIMIT_MAX:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"limit must be an integer 1..{HISTORY_LIMIT_MAX}, got {limit!r}",
            detail={"limit": limit},
        )
    return limit


def budget_report(
    rt: Runtime, *, account: str | None = None, now: datetime | None = None
) -> Report:
    """Budgets, today's posts, quiet hours and unresolved writes per account. Offline."""
    now = now or datetime.now(UTC)
    aliases = [rt.account(account).alias] if account is not None else sorted(rt.registry.accounts())
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
        "home": str(rt.paths.home),
        "default_account": rt.settings.default_account,
        "note": "day and month spend is across every account; posts are per account",
        "accounts": entries,
    }
    return out, 0


def history_report(
    rt: Runtime, *, account: str | None = None, limit: int = HISTORY_LIMIT_DEFAULT
) -> Report:
    """The newest ledger rows, with how many matched in all."""
    limit = check_limit(limit)
    alias = rt.account(account).alias if account is not None else None
    rows = rt.ledger.history(limit=limit, account_alias=alias)
    total = rt.ledger.count(account_alias=alias)
    return {
        "writes": [r.to_dict() for r in rows],
        "total": total,
        "truncated": total > len(rows),
    }, 0


async def validate_report(rt: Runtime, plan_path: Path, *, account: str | None = None) -> Report:
    """What ``publish`` would send, per account, with digests and cost. Offline."""
    plan, targets = rt.plan_targets(read_plan(plan_path), account)
    reports = [rt.publisher.prepare(plan, rt.offline_bound(a)).report() for a in targets]
    return {"valid": True, "published": False, "accounts": reports}, 0


async def publish_report(
    rt: Runtime,
    plan_path: Path,
    *,
    account: str | None = None,
    idempotency_key: str | None = None,
    caller: str | None = None,
    confirm: bool = False,
) -> Report:
    """Publish a plan to each of its accounts. Without ``confirm`` it only validates.

    Every account's plan is prepared (bound, identity checked, validated,
    media loaded) before the first one is published, so a problem found
    offline in a later account's variant stops the command before anything
    is sent. ``rt`` is read-only for a preview.
    """
    if not confirm:
        return _publish_preview(rt, plan_path, account, idempotency_key, caller)
    results: list[dict[str, Any]] = []
    code = 0
    who = rt.caller(caller, default=CLI_CALLER)
    key = check_key(idempotency_key)
    # Registry reads and media hashing go to a thread.
    plan, targets = await asyncio.to_thread(rt.plan_targets, read_plan(plan_path), account)
    key = _plan_key(key, plan)
    _one_account_per_key(key, targets)
    prepared: list[Prepared] = []
    for alias in targets:
        bound = await rt.bound(alias)
        prepared.append(await asyncio.to_thread(rt.publisher.prepare, plan, bound))
    for ready in prepared:
        alias = ready.bound.alias
        try:
            async with rt.watch_expiry(alias):
                outcome = await rt.publisher.publish(ready, idempotency_key=key, caller=who)
        except PulsarError as exc:
            results.append(refused_entry(ready, key, exc))
            code = 1
            continue
        results.append(receipt_entry(outcome))
        if not settled(outcome):
            code = 1
    return {"published": True, "home": str(rt.paths.home), "results": results}, code


def receipt_entry(outcome: Outcome) -> dict[str, Any]:
    """One account's result of a publish: its receipt, ``ok`` and ``error``."""
    entry: dict[str, Any] = {"ok": outcome.error is None, **outcome.receipt()}
    entry["error"] = outcome.error.to_result() if outcome.error is not None else None
    return entry


def refused_entry(ready: Prepared, key: str | None, exc: PulsarError) -> dict[str, Any]:
    """A publish refused before anything was claimed, in a receipt's shape."""
    return {
        "ok": False,
        "idempotency_key": key,
        "state": None,
        "account": ready.bound.alias,
        "digest": ready.digest,
        "replayed": False,
        "items": [],
        "note": None,
        "error": exc.to_result(),
    }


def settled(outcome: Outcome) -> bool:
    return outcome.error is None and outcome.record.state in (PUBLISHED, SKIPPED)


def _publish_preview(
    rt: Runtime,
    plan_path: Path,
    account: str | None,
    idempotency_key: str | None,
    caller: str | None,
) -> Report:
    """``publish`` without ``--confirm``: every offline check the live run
    makes, in its order (the caller label, the key, the plan, the schedule
    and the policy), and nothing written."""
    rt.caller(caller, default=CLI_CALLER)
    key = check_key(idempotency_key)
    plan, targets = rt.plan_targets(read_plan(plan_path), account)
    key = _plan_key(key, plan)
    _one_account_per_key(key, targets)
    reports: list[dict[str, Any]] = []
    for alias in targets:
        ready = rt.publisher.prepare(plan, rt.offline_bound(alias))
        rt.publisher.preflight(ready, idempotency_key=key)
        reports.append(ready.report())
    return {
        "valid": True,
        "published": False,
        "accounts": reports,
        "note": "validated only; re-run with --confirm to publish (this costs money)",
    }, 0


def _plan_key(key: str | None, plan: Plan) -> str | None:
    """The key to publish under: ``--key``, else the plan's own ``key``. They
    must agree when both are given, so neither silently wins."""
    if key is not None and plan.key is not None and key != plan.key:
        raise PulsarError(
            INVALID_ARGUMENT,
            "the idempotency key differs from the plan's `key`; drop one",
            detail={"idempotency_key": key, "plan_key": plan.key},
        )
    return key if key is not None else plan.key


def _one_account_per_key(key: str | None, targets: list[str]) -> None:
    if key is not None and len(targets) > 1:
        raise PulsarError(
            INVALID_ARGUMENT,
            "an idempotency key names one account's write; pick one with --account",
            detail={"accounts": targets},
        )


async def reconcile_report(
    rt: Runtime, *, account: str | None = None, now: datetime | None = None
) -> Report:
    """Settle an account's unknown writes from its timeline.

    Calls the provider only when the account has something to settle; the
    timeline read is billed per post returned. Exit 0 only when every row
    it looked at is settled.
    """
    home = str(rt.paths.home)
    alias = (await asyncio.to_thread(rt.account, account)).alias
    pending = [
        r.key
        for r in rt.ledger.unresolved(stale_after=STALE_SUBMITTING, now=now or datetime.now(UTC))
        if r.account_alias == alias
    ]
    if not pending:
        return {"account": alias, "home": home, "results": []}, 0
    bound = await rt.bound(alias)
    async with rt.watch_expiry(alias):
        results = await rt.publisher.reconcile(bound)
    # ``changed``: a live sender touched the row meanwhile; reconcile left it alone.
    settled = all(
        r["error"] is None and r["state"] != "changed" and is_settled(State(r["state"]))
        for r in results
    )
    return {"account": alias, "home": home, "results": results}, 0 if settled else 1


async def import_report(
    rt: Runtime, source: Path, *, account: str | None = None, confirm: bool = False
) -> Report:
    """Import a retired routine's ``posted.jsonl`` as the account's rows. Idempotent.

    Without ``confirm`` it reports what the import would do and writes nothing
    (``rt`` is read-only then).
    """
    found = await rt.identity(account) if confirm else await asyncio.to_thread(rt.account, account)
    if confirm:
        require_expected(found, rt.settings)
    if alias_provider(found.alias) != "x":
        raise PulsarError(UNSUPPORTED, "posted.jsonl import is for X accounts")
    handle = found.handle or found.alias.partition(":")[2]
    ref = AccountRef(
        alias=found.alias,
        provider="x",
        user_id=found.provider_user_id or "",
        handle=handle,
    )
    try:
        report = import_posted(
            rt.ledger,
            source,
            account=ref,
            url_for=lambda post_id: post_url(handle, post_id),
            apply=confirm,
        )
    except OSError as exc:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"cannot read {source}: {exc.strerror}",
            detail={"source": str(source)},
        ) from exc
    out = {
        "account": ref.alias,
        "source": str(source),
        "home": str(rt.paths.home),
        **report.to_dict(),
        "note": None if confirm else "report only; re-run with --confirm to import",
    }
    return out, 0 if not report.conflicts and not report.errors else 1


def migrate_report(rt: Runtime, *, confirm: bool = False) -> Report:
    """Bring the home up to date in place: the ledger schema, then the
    phase 1 credential layout. Without ``confirm`` (``rt`` read-only) it
    reports what it would do and changes nothing: an upgraded ledger is
    refused by an older pulsar, and moved credentials do not move back."""
    home = str(rt.paths.home)
    if not confirm:
        current, target = rt.ledger.schema_versions()
        legacy = rt.registry.legacy_status(rt.settings)
        pending = current < target or legacy.state == "pending"
        out = {
            "applied": False,
            "home": home,
            "ledger": {"from_version": current, "to_version": target},
            "credentials": _migration(legacy),
            "note": "pass --confirm to apply" if pending else "nothing to migrate",
        }
        return out, 0 if legacy.state in ("pending", "none") else 1
    from_version, to_version = rt.ledger.migrate()
    legacy = rt.registry.migrate_legacy(rt.settings)
    out = {
        "applied": True,
        "home": home,
        "ledger": {"from_version": from_version, "to_version": to_version},
        "credentials": _migration(legacy),
        "note": None,
    }
    return out, 0 if legacy.state in ("migrated", "none") else 1


def _migration(result: MigrationResult) -> dict[str, Any]:
    return {
        "state": result.state,
        "alias": result.alias,
        "adopted": list(result.adopted),
        "message": result.message,
    }
