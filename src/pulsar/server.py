"""The MCP surface: five tools, no secret parameters, structured errors.

Every tool returns a JSON object. Success carries ``ok: true``; failure
carries ``ok: false`` plus a machine-readable ``code`` from ``errors.py``
so the calling agent can branch (re-auth, retry later, rewrite text)
instead of parsing prose.

pulsar does not decide whether a post *should* go out — that is the caller's
policy. What it does is make the policy boundary legible to a harness that
gates by tool name and annotations: ``whoami`` and ``validate_post`` are
``readOnlyHint`` (safe to auto-allow), ``create_post`` and ``upload_media``
publish (not read-only, not destructive), and ``delete_post`` is
``destructiveHint``. ``create_post(dry_run=True)`` still exists for callers
that predate ``validate_post``, but a harness cannot tell it apart from a
live post by name — prefer ``validate_post``.

Every live write is claimed in the ledger (``ledger.py``) before its request
leaves and settled after, so a repeat with the same idempotency key replays
the receipt instead of posting (and paying) twice, and a write whose outcome
is unknowable is reported as ``outcome_unknown`` rather than a retryable
error.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .config import Paths, default_paths
from .errors import API_ERROR, INVALID_TEXT, AuthExpired, OutcomeUnknown, PulsarError
from .guard import validate_text
from .ledger import PUBLISHED, Ledger, check_key, default_key, request_digest
from .media import load_media
from .settings import Prices, Settings, load_settings
from .store import TokenStore, cached_identity, save_identity
from .writelog import WriteLog, resolve_caller, text_sha256
from .xapi import MediaProcessingError, XClient, check_x_id

log = logging.getLogger(__name__)

TOOL_NAMES = ("whoami", "validate_post", "create_post", "upload_media", "delete_post")

# Hints a policy layer can gate on without knowing anything pulsar-specific.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
PUBLISHES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

INSTRUCTIONS = (
    "pulsar posts to X as the account a human authorized on this host. "
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


class Runtime:
    """Everything the tools need, built once per process (or per test)."""

    def __init__(
        self,
        paths: Paths | None = None,
        *,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.paths = paths or default_paths()
        self.settings = settings or load_settings(self.paths)
        self.store = TokenStore(self.paths)
        self.client = XClient(self.store, transport=transport, **client_kwargs)
        self.log = WriteLog(self.paths)
        self.ledger = Ledger(self.paths, export=self.log.export)

    async def whoami(self, *, live: bool = False) -> dict[str, str]:
        """The bound account: cached after the first call, from X when ``live``.

        The cache is tagged with the binding it was looked up under, so a
        lookup that raced a re-login is ignored rather than trusted.
        """
        bundle = self.store.load()
        if bundle is None:
            raise AuthExpired("no X authorization on this host; run `pulsar auth login`")
        if not live and (cached := cached_identity(self.paths, bundle)) is not None:
            return cached
        me = await self.client.me()
        save_identity(self.paths, bundle.binding_id, me)
        return me


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
                API_ERROR, f"internal error: {exc.__class__.__name__}", retryable=False
            ).to_result()

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__annotations__ = fn.__annotations__
    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _validate(
    text: str, reply_to_post_id: str | None, quote_post_id: str | None, prices: Prices
) -> dict[str, Any]:
    report = validate_text(text, prices)
    if reply_to_post_id and quote_post_id:
        raise PulsarError(INVALID_TEXT, "a post cannot be both a reply and a quote in v1")
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


def build_server(runtime: Runtime | None = None) -> MCPServer:
    rt = runtime or Runtime()
    server = MCPServer(name="pulsar", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Return the X account this connector is bound to: {user_id, username}. "
            "Local after the first call."
        ),
        annotations=READ_ONLY,
    )
    @_guarded
    async def whoami() -> dict[str, Any]:
        me = await rt.whoami()
        return {"ok": True, **me}

    @server.tool(
        description=(
            "Validate post text without publishing: weighted length (<=280), URL detection, "
            "credential scan, reply/quote conflict, and an estimated_cost_usd. Never touches "
            "the network. Safe to call freely; use it before create_post."
        ),
        annotations=READ_ONLY,
    )
    @_guarded
    async def validate_post(
        text: str,
        reply_to_post_id: str | None = None,
        quote_post_id: str | None = None,
    ) -> dict[str, Any]:
        return _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)

    @server.tool(
        description=(
            "Create a post on X as the bound account. Requires explicit user intent in the "
            "calling chat or an owner-enabled routine. Text is validated (<=280 weighted chars, "
            "no credential-looking strings) before any network call. Idempotent per "
            "idempotency_key: a repeat returns the stored receipt with replayed=true and does "
            "not post. On outcome_unknown the post may be live: do not retry. dry_run=true is "
            "the legacy validate-only path; prefer validate_post, which a policy layer can gate "
            "separately. `caller` is an advisory audit label, not identity."
        ),
        annotations=PUBLISHES,
    )
    @_guarded
    async def create_post(
        text: str,
        reply_to_post_id: str | None = None,
        quote_post_id: str | None = None,
        media_ids: list[str] | None = None,
        dry_run: bool = False,
        caller: Caller = None,
        idempotency_key: IdempotencyKey = None,
    ) -> dict[str, Any]:
        validated = _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)
        media_ids = [check_x_id(m, "media_ids") for m in media_ids or []] or None
        key = check_key(idempotency_key)
        if dry_run:
            # Not a write: nothing reaches the ledger or writes.jsonl.
            return {**validated, "dry_run": True}
        me = await rt.whoami()
        digest = request_digest(
            "create_post",
            text=text,
            reply_to_post_id=str(reply_to_post_id).strip() if reply_to_post_id else None,
            quote_post_id=str(quote_post_id).strip() if quote_post_id else None,
            media_ids=media_ids or [],
        )
        key = key or default_key(digest, me["user_id"])
        record = rt.ledger.claim(
            key=key,
            tool="create_post",
            digest=digest,
            account=me,
            caller=resolve_caller(caller),
            text_sha256=text_sha256(text),
        )
        if record.state == PUBLISHED:
            return {
                "ok": True,
                "post_id": record.post_id,
                "url": record.url,
                "text": text,
                "replayed": True,
            }
        created = await _settle_on_error(
            rt,
            key,
            lambda: rt.client.create_post(
                text,
                reply_to_post_id=reply_to_post_id,
                quote_post_id=quote_post_id,
                media_ids=media_ids,
            ),
            ambiguous=True,
        )
        url = f"https://x.com/{me['username']}/status/{created['post_id']}"
        _record_success(rt, key, post_id=created["post_id"], url=url)
        return {"ok": True, "post_id": created["post_id"], "url": url, "text": created["text"]}

    @server.tool(
        description=(
            "Upload an image (png/jpeg/gif/webp, <=5 MiB) or MP4 video "
            "(video/mp4, <=100 MiB) for a later create_post. Pass `path` (a regular file "
            "inside the operator's configured media roots; relative paths are from the "
            "server's cwd; refused as invalid_config when no roots are set) or `base64`. "
            "The type is sniffed from the content; a `mime` or extension that "
            "disagrees is refused. Video waits for X processing to succeed. Returns {media_id}. "
            "`caller` is an advisory audit label, not identity."
        ),
        annotations=PUBLISHES,
    )
    @_guarded
    async def upload_media(
        path: str | None = None,
        base64: str | None = None,
        mime: str | None = None,
        caller: Caller = None,
    ) -> dict[str, Any]:
        data, resolved_mime = load_media(
            path, base64, mime, roots=rt.settings.media_roots, deny=(rt.paths.home,)
        )
        me = await rt.whoami()
        facts = {"mime": resolved_mime, "bytes": len(data)}
        # Uploads are not deduplicated: an orphaned media id is harmless and
        # expires, so every call is its own ledger row.
        key = f"upload:{uuid.uuid4().hex}"
        rt.ledger.claim(
            key=key,
            tool="upload_media",
            digest=request_digest("upload_media", **facts, sha256=hashlib.sha256(data).hexdigest()),
            account=me,
            caller=resolve_caller(caller),
            meta=facts,
        )
        media_id, processing_state = await _settle_on_error(
            rt,
            key,
            lambda: rt.client.upload_media(data, resolved_mime),
            ambiguous=False,
            error_meta=lambda exc: {
                "processing_state": (
                    exc.processing_state if isinstance(exc, MediaProcessingError) else "error"
                )
            },
        )
        _record_success(rt, key, media_id=media_id, meta={"processing_state": processing_state})
        return {"ok": True, "media_id": media_id, "mime": resolved_mime, "bytes": len(data)}

    @server.tool(
        description=(
            "Delete a post by its numeric X id. Only posts made by the bound account can be "
            "deleted. "
            "Repeating a delete that already succeeded returns the stored receipt "
            "(replayed: true). `caller` is an advisory audit label, not identity."
        ),
        annotations=DESTRUCTIVE,
    )
    @_guarded
    async def delete_post(
        post_id: str, caller: Caller = None, idempotency_key: IdempotencyKey = None
    ) -> dict[str, Any]:
        post_id = check_x_id(post_id, "post_id")
        key = check_key(idempotency_key) or f"delete:{post_id}"
        me = await rt.whoami()
        record = rt.ledger.claim(
            key=key,
            tool="delete_post",
            digest=request_digest("delete_post", post_id=post_id),
            account=me,
            caller=resolve_caller(caller),
        )
        if record.state == PUBLISHED:
            deleted = record.meta.get("deleted", True)
            return {"ok": True, "post_id": post_id, "deleted": deleted, "replayed": True}
        # DELETE is idempotent at X, so transport failures stay retryable.
        deleted = await _settle_on_error(
            rt, key, lambda: rt.client.delete_post(post_id), ambiguous=False
        )
        _record_success(rt, key, post_id=post_id, meta={"deleted": deleted})
        return {"ok": True, "post_id": post_id, "deleted": deleted}

    return server


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
        rt.ledger.fail(key, exc, meta=error_meta(exc) if error_meta else None)
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
        rt.ledger.fail(key, err, meta=error_meta(err) if error_meta else None)
        if isinstance(exc, Exception):
            raise err from exc
        raise


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
