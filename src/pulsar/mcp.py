"""The MCP surface: six tools, no secret parameters, structured errors.

Every tool returns a JSON object. Success carries ``ok: true``; failure
carries ``ok: false`` plus a machine-readable ``code`` from ``errors.py``
so the calling agent can branch (re-auth, retry later, rewrite text)
instead of parsing prose.

pulsar does not decide whether a post *should* go out — that is the caller's
policy. What it does is make the policy boundary legible to a harness that
gates by tool name and annotations: ``whoami``, ``validate_post`` and
``validate_plan`` are ``readOnlyHint`` (safe to auto-allow), ``create_post`` and ``upload_media``
publish (not read-only, not destructive), and ``delete_post`` is
``destructiveHint``. ``create_post(dry_run=True)`` still exists for callers
that predate ``validate_post``, but a harness cannot tell it apart from a
live post by name — prefer ``validate_post``.

Every live write is claimed in the ledger (``core/ledger/``) before its request
leaves and settled after, so a repeat with the same idempotency key replays
the receipt instead of posting (and paying) twice, and a write whose outcome
is unknowable is reported as ``outcome_unknown`` rather than a retryable
error.

The account tools act as is resolved per call from the registry
(``core/accounts.py``): the ``account`` argument, else the configured
default. Before every write the bound handle is checked against the alias
and ``expected_handle`` (``account_mismatch``), so a wrong token stored under
the right name cannot post.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field

from pulsar import __version__
from pulsar.app import (
    API_ERROR,
    IDEMPOTENCY_CONFLICT,
    INTERNAL,
    MAX_IMAGE_BYTES,
    MAX_VIDEO_BYTES,
    PUBLISHED,
    SKIPPED,
    STALE_SUBMITTING,
    Bound,
    MediaProcessingError,
    Outcome,
    OutcomeUnknown,
    Plan,
    Prepared,
    Prices,
    PulsarError,
    Runtime,
    check_key,
    check_x_id,
    load_media,
    request_digest,
    validate_text,
)

log = logging.getLogger(__name__)

TOOL_NAMES = (
    "whoami",
    "validate_post",
    "validate_plan",
    "create_post",
    "upload_media",
    "delete_post",
)

# The names a loopback HTTP server answers to, by bind address (`pulsar serve` binds nothing else).
LOOPBACK_NAMES: dict[str, tuple[str, ...]] = {
    "127.0.0.1": ("127.0.0.1", "localhost"),
    "localhost": ("127.0.0.1", "localhost"),
    "::1": ("[::1]", "localhost"),
}

# Hints a policy layer can gate on without knowing anything pulsar-specific.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
PUBLISHES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True)

INSTRUCTIONS = (
    "pulsar posts to X as the accounts a human bound on this host; pass `account` "
    "(provider:handle) to pick one, or omit it for the operator's default. "
    "Call create_post only on explicit user intent in the current conversation "
    "or from a standing routine the owner enabled. Never pass credentials; there "
    "is no parameter for them. Use validate_post to check text before posting. "
    "If a write returns outcome_unknown, do not retry it: the post may be live."
)

# Parameter schemas shared by the writing tools. Module-level so the string
# annotations (``from __future__ import annotations``) resolve.
Caller = Annotated[
    str | None,
    Field(
        description=(
            "Advisory label for the calling agent, recorded in the ledger. Self-asserted "
            "audit, not identity: pulsar does not verify it. Falls back to PULSAR_CALLER."
        )
    ),
]
IdempotencyKey = Annotated[
    str | None,
    Field(
        description=(
            "Optional 1-200 char key with no whitespace. A repeat call with the same key and "
            "the same request returns the stored receipt (replayed: true) without calling X; "
            "the same key with a different request is idempotency_conflict. Default: derived "
            "from the request and the bound account."
        )
    ),
]


AccountArg = Annotated[
    str | None,
    Field(
        description=(
            "Alias of a bound account, provider:handle (e.g. x:<handle>). Default: "
            "default_account from the operator's config, else the only bound account."
        )
    ),
]

PostText = Annotated[
    str,
    Field(
        description="The post's text: at most 280 weighted characters (a URL counts 23, CJK "
        "and emoji 2), no control characters, nothing that looks like a credential."
    ),
]
ReplyTo = Annotated[
    str | None,
    Field(description="Id of the post this one replies to (digits). Not with quote_post_id."),
]
QuoteOf = Annotated[
    str | None,
    Field(description="Id of the post this one quotes (digits). Not with reply_to_post_id."),
]
MediaIds = Annotated[
    list[str] | None,
    Field(description="Media ids from upload_media, uploaded as the same account; at most 4."),
]
DryRun = Annotated[
    bool,
    Field(
        description="Legacy: validate and check policy only, send and record nothing. "
        "Prefer validate_post."
    ),
]
PostId = Annotated[str, Field(description="Id of the post to delete (digits).")]
MediaPath = Annotated[
    str | None,
    Field(
        description="A file under the operator's configured media roots; a relative path "
        "starts at the server's working directory. Exactly one of path or base64."
    ),
]
MediaBase64 = Annotated[
    str | None,
    Field(description="The file's bytes, base64-encoded. Exactly one of path or base64."),
]
MediaMime = Annotated[
    str | None,
    Field(
        description="image/png, image/jpeg, image/gif, image/webp or video/mp4. Default: "
        "sniffed from the bytes; when given it must agree with them."
    ),
]
PlanArg = Annotated[
    dict[str, Any],
    Field(
        description="A plan: {account | accounts, text | posts[{text, media[{path, alt}]}], "
        "reply_to | quote, variants, not_before}."
    ),
]


def _guarded(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except PulsarError as exc:
            return exc.to_result()
        except Exception as exc:
            # A bug or an unexpected shape from X/disk. Writes already settle
            # their ledger row (outcome_unknown once a post may be in flight),
            # so this only turns a traceback into a result; not retryable,
            # because nothing says a repeat would go differently.
            log.exception("pulsar: unexpected error in %s", fn.__name__)
            return PulsarError(
                INTERNAL, f"internal error: {exc.__class__.__name__}", retryable=False
            ).to_result()

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__annotations__ = fn.__annotations__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _strict(tool: Tool) -> Tool:
    """Refuse arguments the tool does not declare.

    The SDK's argument models ignore unknown keys, so a misspelt ``acount``
    would post as the default account. Forbidding extras makes it a
    validation error, and ``additionalProperties: false`` advertises that.
    """
    base = tool.fn_metadata.arg_model
    tool.fn_metadata.arg_model = type(
        base.__name__,
        (base,),
        {"model_config": ConfigDict(arbitrary_types_allowed=True, extra="forbid")},
    )
    tool.parameters = {**tool.parameters, "additionalProperties": False}
    return tool


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
) -> dict[str, Any]:
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


def build_server(rt: Runtime) -> MCPServer:

    @_guarded
    async def whoami(account: AccountArg = None) -> dict[str, Any]:
        me = await rt.whoami(account)
        return {"ok": True, **me}

    @_guarded
    async def validate_post(
        text: PostText,
        reply_to_post_id: ReplyTo = None,
        quote_post_id: QuoteOf = None,
    ) -> dict[str, Any]:
        return _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)

    @_guarded
    async def validate_plan(plan: PlanArg, account: AccountArg = None) -> dict[str, Any]:
        def check() -> list[dict[str, Any]]:
            parsed, targets = rt.plan_targets(Plan.from_mapping(plan), account)
            return [rt.publisher.prepare(parsed, rt.offline_bound(a)).report() for a in targets]

        # Media reads and hashing are blocking; keep them off the event loop.
        return {"ok": True, "accounts": await asyncio.to_thread(check)}

    @_guarded
    async def create_post(
        text: PostText,
        reply_to_post_id: ReplyTo = None,
        quote_post_id: QuoteOf = None,
        media_ids: MediaIds = None,
        dry_run: DryRun = False,
        caller: Caller = None,
        idempotency_key: IdempotencyKey = None,
        account: AccountArg = None,
    ) -> dict[str, Any]:
        validated = _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)
        media_ids = [check_x_id(m, "media_ids") for m in media_ids or []] or None
        key = check_key(idempotency_key)
        who = rt.caller(caller)
        if dry_run:
            # Not a write: nothing reaches the ledger or writes.jsonl. The same
            # offline checks as the live call, in the same order: the account, the plan, the policy.
            alias = (await asyncio.to_thread(rt.account, account)).alias
            prepared = await asyncio.to_thread(
                _legacy_post,
                rt,
                rt.offline_bound(alias),
                text,
                reply_to_post_id,
                quote_post_id,
                media_ids or [],
            )
            await asyncio.to_thread(rt.publisher.preflight, prepared, idempotency_key=key)
            return {**validated, "dry_run": True}
        bound = await rt.bound(account)
        prepared = await asyncio.to_thread(
            _legacy_post, rt, bound, text, reply_to_post_id, quote_post_id, media_ids or []
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

    @_guarded
    async def upload_media(
        path: MediaPath = None,
        base64: MediaBase64 = None,
        mime: MediaMime = None,
        caller: Caller = None,
        account: AccountArg = None,
    ) -> dict[str, Any]:
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

    @_guarded
    async def delete_post(
        post_id: PostId,
        caller: Caller = None,
        idempotency_key: IdempotencyKey = None,
        account: AccountArg = None,
    ) -> dict[str, Any]:
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

    tools = [
        Tool.from_function(
            whoami,
            description=(
                "Return the X account behind `account` (default: the operator's default "
                "account): {user_id, username}. Local after the first call."
            ),
            annotations=READ_ONLY,
        ),
        Tool.from_function(
            validate_post,
            description=(
                "Validate post text without publishing: weighted length (<=280), URL "
                "detection, credential scan, reply/quote conflict, and an "
                "estimated_cost_usd. Never touches the network. Safe to call freely; use it "
                "before create_post."
            ),
            annotations=READ_ONLY,
        ),
        Tool.from_function(
            validate_plan,
            description=(
                "Validate a plan without publishing: {account|accounts, text | posts[{text, "
                "media[{path, alt}]}], reply_to|quote, variants{provider: {posts}}, "
                "not_before}. Checks each post against the account's provider (length, media "
                "type, size and count, alt text, credential scan), loads media under the "
                "operator's media roots, and returns per account the digest and "
                "estimated_cost_usd. Never touches the network. `account` picks one of the "
                "plan's accounts, or binds a plan that names none (default: the operator's "
                "default account)."
            ),
            annotations=READ_ONLY,
        ),
        Tool.from_function(
            create_post,
            description=(
                "Create a post on X as `account` (default: the operator's default account); "
                "refused with account_mismatch if its credentials belong to another handle. "
                "Requires explicit user intent in the calling chat or an owner-enabled "
                "routine. Text is validated (<=280 weighted chars, no credential-looking "
                "strings) before any network call. Idempotent per idempotency_key: a repeat "
                "returns the stored receipt with replayed=true and does not post. On "
                "outcome_unknown the post may be live: do not retry. dry_run=true is the "
                "legacy validate-only path (same offline checks, nothing sent); prefer "
                "validate_post, which a policy layer can gate separately. The operator's "
                "policy applies: budget_exceeded, daily_cap or quiet_hours come back before "
                "anything is sent, with detail.retry_after. `caller` is an advisory audit "
                "label, not identity."
            ),
            annotations=PUBLISHES,
        ),
        Tool.from_function(
            upload_media,
            description=(
                f"Upload an image (png/jpeg/gif/webp, <={_mib(MAX_IMAGE_BYTES)} MiB) or MP4 "
                f"video (video/mp4, <={_mib(MAX_VIDEO_BYTES)} MiB) for a later create_post. "
                "Pass `path` (a regular file inside the operator's configured media roots; "
                "relative paths are from the server's cwd; refused as invalid_config when no "
                "roots are set) or `base64`. The type is sniffed from the content; a `mime` or "
                "extension that disagrees is refused. Video waits for X processing to "
                "succeed. Returns {media_id}. Upload as the same `account` that will post the "
                "media. `caller` is an advisory audit label, not identity."
            ),
            annotations=PUBLISHES,
        ),
        Tool.from_function(
            delete_post,
            description=(
                "Delete a post by its numeric X id, as `account` (default: the operator's "
                "default account); only that account's own posts can be deleted. Repeating a "
                "delete that already succeeded returns the stored receipt (replayed: true). "
                "`caller` is an advisory audit label, not identity."
            ),
            annotations=DESTRUCTIVE,
        ),
    ]
    return MCPServer(
        name="pulsar",
        version=__version__,
        instructions=INSTRUCTIONS,
        tools=[_strict(t) for t in tools],
    )


def _mib(limit: int) -> int:
    return limit // (1024 * 1024)


def loopback_security(host: str, port: int) -> TransportSecuritySettings:
    """Host and Origin checks for the loopback HTTP transport.

    Only the exact authority the server is bound to, and an ``http`` Origin
    naming that same authority, are accepted; the SDK's own default allows
    any port.
    """
    authorities = [f"{name}:{port}" for name in LOOPBACK_NAMES[host]]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=authorities,
        allowed_origins=[f"http://{a}" for a in authorities],
    )


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


def _legacy_receipt(outcome: Outcome, prepared: Prepared) -> dict[str, Any]:
    """The phase 1 receipt shape, plus ``replayed`` (always present)."""
    live = outcome.live.get(0)
    item = outcome.record.items[0]
    out: dict[str, Any] = {
        "ok": True,
        "post_id": live.post_id if live else item.post_id,
        "url": live.url if live else item.url,
        "text": live.text if live else prepared.posts[0].check.text,
        "replayed": outcome.replayed,
    }
    return out


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
