"""The agent tools behind ``pulsar serve``: ``whoami``, ``validate_post``,
``validate_plan``, ``create_post``, ``upload_media`` and ``delete_post``.

Each takes the ``Runtime`` it runs against and returns the tool's result as
a JSON object (``ok: true``), or raises ``PulsarError``; the MCP server
(``pulsar.mcp``) owns the parameter schemas, the annotations and turning an
error into a result.

Every live write is claimed in the ledger before its request leaves and
settled after, so a repeat with the same idempotency key replays the receipt
instead of posting (and paying) twice, and a write whose outcome is
unknowable is reported as ``outcome_unknown`` rather than a retryable error.

The account a tool acts as is resolved per call from the registry: the
``account`` argument, else the configured default. Before every write the
bound handle is checked against the alias and ``expected_handle``
(``account_mismatch``), so a wrong token stored under the right name cannot
post.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pulsar.app.core.channels.contract import MAX_IMAGE_BYTES, MAX_VIDEO_BYTES, Prices
from pulsar.app.core.channels.x import MediaProcessingError, check_x_id, validate_text
from pulsar.app.core.ledger import PUBLISHED, SKIPPED, check_key, request_digest
from pulsar.app.core.publishing import (
    STALE_SUBMITTING,
    Bound,
    Outcome,
    Plan,
    Prepared,
    load_media,
)
from pulsar.internal.errors import API_ERROR, IDEMPOTENCY_CONFLICT, OutcomeUnknown, PulsarError

from .interfaces import Runtime

log = logging.getLogger(__name__)

Result = dict[str, Any]

# What upload_media accepts, in MiB, for the tool's description.
MAX_IMAGE_MIB = MAX_IMAGE_BYTES // (1024 * 1024)
MAX_VIDEO_MIB = MAX_VIDEO_BYTES // (1024 * 1024)


async def whoami(rt: Runtime, account: str | None) -> Result:
    me = await rt.whoami(account)
    return {"ok": True, **me}


def validate_post(
    rt: Runtime, text: str, reply_to_post_id: str | None, quote_post_id: str | None
) -> Result:
    return _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)


async def validate_plan(rt: Runtime, plan: dict[str, Any], account: str | None) -> Result:
    def check() -> list[dict[str, Any]]:
        parsed, targets = rt.plan_targets(Plan.from_mapping(plan), account)
        return [rt.publisher.prepare(parsed, rt.offline_bound(a)).report() for a in targets]

    # Media reads and hashing are blocking; keep them off the event loop.
    return {"ok": True, "accounts": await asyncio.to_thread(check)}


async def create_post(
    rt: Runtime,
    *,
    text: str,
    reply_to_post_id: str | None,
    quote_post_id: str | None,
    media_ids: list[str] | None,
    dry_run: bool,
    caller: str | None,
    idempotency_key: str | None,
    account: str | None,
) -> Result:
    validated = _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)
    media = [check_x_id(m, "media_ids") for m in media_ids or []]
    key = check_key(idempotency_key)
    who = rt.caller(caller)
    if dry_run:
        # Not a write: nothing reaches the ledger or writes.jsonl. The same
        # offline checks as the live call, in the same order: the account, the plan, the policy.
        alias = (await asyncio.to_thread(rt.account, account)).alias
        prepared = await asyncio.to_thread(
            _legacy_post, rt, rt.offline_bound(alias), text, reply_to_post_id, quote_post_id, media
        )
        await asyncio.to_thread(rt.publisher.preflight, prepared, idempotency_key=key)
        return {**validated, "dry_run": True}
    bound = await rt.bound(account)
    prepared = await asyncio.to_thread(
        _legacy_post, rt, bound, text, reply_to_post_id, quote_post_id, media
    )
    async with rt.watch_expiry(bound.alias):
        outcome = await rt.publisher.publish(
            prepared, idempotency_key=key, caller=who, tool="create_post"
        )
    if outcome.error is not None:
        raise outcome.error
    if outcome.record.state == SKIPPED:
        raise PulsarError(
            IDEMPOTENCY_CONFLICT,
            "the operator marked this idempotency_key skipped; nothing was posted",
            detail={"idempotency_key": outcome.record.key, "state": SKIPPED},
        )
    return _legacy_receipt(outcome, prepared)


async def upload_media(
    rt: Runtime,
    *,
    path: str | None,
    base64: str | None,
    mime: str | None,
    caller: str | None,
    account: str | None,
) -> Result:
    who = rt.caller(caller)
    data, resolved_mime = await asyncio.to_thread(
        load_media,
        path,
        base64,
        mime,
        roots=rt.settings.media_roots,
        deny=(rt.paths.home,),
        base=rt.publisher.media_base,
    )
    acct, client, me = await rt.writer(account)
    facts = {"mime": resolved_mime, "bytes": len(data)}
    # Uploads are not deduplicated: an orphaned media id is harmless and
    # expires, so every call is its own ledger row.
    key = f"upload:{uuid.uuid4().hex}"
    await asyncio.to_thread(
        rt.ledger.claim,
        key=key,
        tool="upload_media",
        # A row left submitting by a process that died is re-armed after this.
        stale_after=STALE_SUBMITTING,
        digest=request_digest("upload_media", **facts, sha256=hashlib.sha256(data).hexdigest()),
        account=me,
        caller=who,
        meta=facts,
    )
    async with rt.watch_expiry(acct.alias):
        media_id, processing_state = await _settle_on_error(
            rt,
            key,
            lambda: client.upload_media(data, resolved_mime),
            ambiguous=False,
            error_meta=lambda exc: {
                "processing_state": (
                    exc.processing_state if isinstance(exc, MediaProcessingError) else "error"
                )
            },
        )
    _record_success(rt, key, media_id=media_id, meta={"processing_state": processing_state})
    return {
        "ok": True,
        "account": acct.alias,
        "media_id": media_id,
        "mime": resolved_mime,
        "bytes": len(data),
    }


async def delete_post(
    rt: Runtime,
    *,
    post_id: str,
    caller: str | None,
    idempotency_key: str | None,
    account: str | None,
) -> Result:
    post_id = check_x_id(post_id, "post_id")
    key = check_key(idempotency_key) or f"delete:{post_id}"
    who = rt.caller(caller)
    acct, client, me = await rt.writer(account)
    record = await asyncio.to_thread(
        rt.ledger.claim,
        key=key,
        tool="delete_post",
        # A row left submitting by a process that died is re-armed after this.
        stale_after=STALE_SUBMITTING,
        digest=request_digest("delete_post", post_id=post_id),
        account=me,
        caller=who,
    )
    if record.state == PUBLISHED:
        deleted = record.meta.get("deleted", True)
        return {
            "ok": True,
            "account": acct.alias,
            "post_id": post_id,
            "deleted": deleted,
            "replayed": True,
        }
    # DELETE is idempotent at X, so transport failures stay retryable.
    async with rt.watch_expiry(acct.alias):
        deleted = await _settle_on_error(
            rt, key, lambda: client.delete_post(post_id), ambiguous=False
        )
    _record_success(rt, key, post_id=post_id, meta={"deleted": deleted})
    return {
        "ok": True,
        "account": acct.alias,
        "post_id": post_id,
        "deleted": deleted,
        "replayed": False,
    }


def _one_post_plan(account: str | None, text: str, reply_to: str | None, quote: str | None) -> Plan:
    """A tool call's post as a plan, so the plan's own rules (one of reply or
    quote, the text checks) are the only definition of them."""
    return Plan.from_mapping(
        {
            **({"account": account} if account else {}),
            "text": text,
            "reply_to": reply_to,
            "quote": quote,
        }
    )


def _validate(
    text: str, reply_to_post_id: str | None, quote_post_id: str | None, prices: Prices
) -> Result:
    _one_post_plan(None, text, reply_to_post_id, quote_post_id)
    report = validate_text(text, prices)
    if reply_to_post_id:
        check_x_id(reply_to_post_id, "reply_to_post_id")
    if quote_post_id:
        check_x_id(quote_post_id, "quote_post_id")
    return {
        "ok": True,
        "text": text,
        "weighted_length": report.weighted_length,
        "has_url": report.has_url,
        "estimated_cost_usd": report.estimated_cost_usd,
        "pricing_note": "from the configured price table; verify on the X developer portal",
    }


def _legacy_post(
    rt: Runtime,
    bound: Bound,
    text: str,
    reply_to: str | None,
    quote: str | None,
    media_ids: list[str],
) -> Prepared:
    """``create_post`` as a one-post plan, so policy, budget and the ledger apply.

    The caller uploaded its media earlier (``upload_media``) and passes ids,
    which a plan cannot express; they ride along as ``uploaded``.

    The digest stays the phase 1 request digest (text as given, reply,
    quote, media ids), not the plan digest: default keys and stored rows
    carry over the upgrade unchanged, so re-sending a post made before it
    replays instead of posting twice, and a row an older process left in
    flight is still reported as in flight.
    """
    plan = Plan.from_mapping(
        {"account": bound.alias, "text": text, "reply_to": reply_to, "quote": quote}
    )
    prepared = rt.publisher.prepare(plan, bound)
    digest = request_digest(
        "create_post",
        text=text,
        reply_to_post_id=str(reply_to).strip() if reply_to else None,
        quote_post_id=str(quote).strip() if quote else None,
        media_ids=media_ids,
    )
    first = replace(prepared.posts[0], uploaded=tuple(media_ids))
    return replace(prepared, digest=digest, posts=(first,))


def _legacy_receipt(outcome: Outcome, prepared: Prepared) -> Result:
    """The phase 1 receipt shape, plus ``replayed`` (always present)."""
    live = outcome.live.get(0)
    item = outcome.record.items[0]
    return {
        "ok": True,
        "post_id": live.post_id if live else item.post_id,
        "url": live.url if live else item.url,
        "text": live.text if live else prepared.posts[0].check.text,
        "replayed": outcome.replayed,
    }


async def _settle_on_error[T](
    rt: Runtime,
    key: str,
    call: Callable[[], Awaitable[T]],
    *,
    ambiguous: bool,
    error_meta: Callable[[PulsarError], dict[str, Any]] | None = None,
) -> T:
    """Run the network half of a claimed write; on any failure, settle its row.

    A ``PulsarError`` settles as ``failed`` or, for ``outcome_unknown``,
    ``unknown``. Anything else (a bug, a cancelled call) happened with the
    request possibly in flight, so for a non-idempotent write it is
    ``outcome_unknown`` too — never a silent ``submitting`` the caller
    cannot see.
    """
    try:
        return await call()
    except PulsarError as exc:
        _settle_failure(rt, key, exc, error_meta)
        if isinstance(exc, OutcomeUnknown):
            exc.detail = {**(exc.detail or {}), "idempotency_key": key}
        raise
    except BaseException as exc:
        name = exc.__class__.__name__
        err = (
            OutcomeUnknown(
                f"{name} while the request was in flight", detail={"idempotency_key": key}
            )
            if ambiguous
            else PulsarError(API_ERROR, f"unexpected {name}", retryable=True)
        )
        _settle_failure(rt, key, err, error_meta)
        if isinstance(exc, Exception):
            raise err from exc
        raise


def _settle_failure(
    rt: Runtime,
    key: str,
    err: PulsarError,
    error_meta: Callable[[PulsarError], dict[str, Any]] | None,
) -> None:
    """Record a failed or unknown outcome. Runs inside an ``except``: a ledger
    failure here is logged, never raised over the error (or cancellation) the
    caller must see; the row stays ``submitting`` for reconcile."""
    try:
        rt.ledger.fail(key, err, meta=error_meta(err) if error_meta else None)
    except Exception:
        log.exception("ledger: could not record %s as %s; row stays submitting", key, err.code)


def _record_success(rt: Runtime, key: str, **fields: Any) -> None:
    """Settle a claimed row as published. The write is live whatever happens
    here, so a ledger failure is logged, never turned into a tool error the
    caller might answer by posting again."""
    try:
        rt.ledger.publish(key, **fields)
    except Exception:
        log.exception(
            "ledger: could not record success for %s (%s); row stays submitting",
            key,
            json.dumps({k: v for k, v in fields.items() if k != "meta"}, sort_keys=True),
        )
