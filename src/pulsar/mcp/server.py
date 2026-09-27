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

The tools themselves live in ``pulsar.app.tools``; this module is their MCP
face: parameter schemas, annotations, and errors turned into results.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ConfigDict, Field

from pulsar import __version__
from pulsar.app import tools as app_tools
from pulsar.app.interfaces import Runtime
from pulsar.internal.errors import INTERNAL, PulsarError

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


def build_server(rt: Runtime) -> MCPServer:

    @_guarded
    async def whoami(account: AccountArg = None) -> dict[str, Any]:
        return await app_tools.whoami(rt, account)

    @_guarded
    async def validate_post(
        text: PostText,
        reply_to_post_id: ReplyTo = None,
        quote_post_id: QuoteOf = None,
    ) -> dict[str, Any]:
        return app_tools.validate_post(rt, text, reply_to_post_id, quote_post_id)

    @_guarded
    async def validate_plan(plan: PlanArg, account: AccountArg = None) -> dict[str, Any]:
        return await app_tools.validate_plan(rt, plan, account)

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
        return await app_tools.create_post(
            rt,
            text=text,
            reply_to_post_id=reply_to_post_id,
            quote_post_id=quote_post_id,
            media_ids=media_ids,
            dry_run=dry_run,
            caller=caller,
            idempotency_key=idempotency_key,
            account=account,
        )

    @_guarded
    async def upload_media(
        path: MediaPath = None,
        base64: MediaBase64 = None,
        mime: MediaMime = None,
        caller: Caller = None,
        account: AccountArg = None,
    ) -> dict[str, Any]:
        return await app_tools.upload_media(
            rt, path=path, base64=base64, mime=mime, caller=caller, account=account
        )

    @_guarded
    async def delete_post(
        post_id: PostId,
        caller: Caller = None,
        idempotency_key: IdempotencyKey = None,
        account: AccountArg = None,
    ) -> dict[str, Any]:
        return await app_tools.delete_post(
            rt, post_id=post_id, caller=caller, idempotency_key=idempotency_key, account=account
        )

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
                f"Upload an image (png/jpeg/gif/webp, <={app_tools.MAX_IMAGE_MIB} MiB) or MP4 "
                f"video (video/mp4, <={app_tools.MAX_VIDEO_MIB} MiB) for a later create_post. "
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
