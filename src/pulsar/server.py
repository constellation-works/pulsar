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
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import __version__
from .config import Paths, default_paths
from .errors import PulsarError
from .guard import validate_text
from .media import load_media
from .settings import Prices, Settings, load_settings
from .store import TokenStore
from .writelog import WriteLog
from .xapi import MediaProcessingError, XClient

TOOL_NAMES = ("whoami", "validate_post", "create_post", "upload_media", "delete_post")

# Hints a policy layer can gate on without knowing anything pulsar-specific.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True)
PUBLISHES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True)

INSTRUCTIONS = (
    "pulsar posts to X as the account a human authorized on this host. "
    "Call create_post only on explicit user intent in the current conversation "
    "or from a standing routine the owner enabled. Never pass credentials; there "
    "is no parameter for them. Use validate_post to check text before posting."
)


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

    async def whoami(self) -> dict[str, str]:
        cache = self.paths.whoami_cache
        if cache.exists():
            try:
                cached = json.loads(cache.read_text())
                if {"user_id", "username"} <= cached.keys():
                    return {"user_id": cached["user_id"], "username": cached["username"]}
            except ValueError:
                pass
        me = await self.client.me()
        self.paths.ensure()
        cache.write_text(json.dumps(me) + "\n")
        return me


def _guarded(
    fn: Callable[..., Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await fn(*args, **kwargs)
        except PulsarError as exc:
            return exc.to_result()

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
        raise PulsarError("invalid_text", "a post cannot be both a reply and a quote in v1")
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
            "no credential-looking strings) before any network call. dry_run=true is the legacy "
            "validate-only path; prefer validate_post, which a policy layer can gate separately."
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
        caller: str | None = None,
    ) -> dict[str, Any]:
        validated = _validate(text, reply_to_post_id, quote_post_id, rt.settings.prices)
        if dry_run:
            rt.log.append(tool="create_post", caller=caller, text=text, dry_run=True)
            return {**validated, "dry_run": True}
        me = await rt.whoami()
        created = await rt.client.create_post(
            text,
            reply_to_post_id=reply_to_post_id,
            quote_post_id=quote_post_id,
            media_ids=media_ids,
        )
        rt.log.append(tool="create_post", caller=caller, text=text, post_id=created["post_id"])
        return {
            "ok": True,
            "post_id": created["post_id"],
            "url": f"https://x.com/{me['username']}/status/{created['post_id']}",
            "text": created["text"],
        }

    @server.tool(
        description=(
            "Upload an image (png/jpeg/gif/webp, <=5 MiB) or MP4 video "
            "(video/mp4, <=100 MiB) for a later create_post. Pass `path` (a regular file "
            "inside the operator's media roots; relative paths are from the server's cwd) "
            "or `base64`. The type is sniffed from the content; a `mime` or extension that "
            "disagrees is refused. Video waits for X processing to succeed. Returns {media_id}."
        ),
        annotations=PUBLISHES,
    )
    @_guarded
    async def upload_media(
        path: str | None = None,
        base64: str | None = None,
        mime: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        data, resolved_mime = load_media(
            path, base64, mime, roots=rt.settings.effective_media_roots(), deny=(rt.paths.home,)
        )
        try:
            media_id, processing_state = await rt.client.upload_media(data, resolved_mime)
        except PulsarError as exc:
            rt.log.append(
                tool="upload_media",
                caller=caller,
                extra={
                    "mime": resolved_mime,
                    "bytes": len(data),
                    "processing_state": (
                        exc.processing_state if isinstance(exc, MediaProcessingError) else "error"
                    ),
                },
            )
            raise
        rt.log.append(
            tool="upload_media",
            caller=caller,
            extra={
                "media_id": media_id,
                "mime": resolved_mime,
                "bytes": len(data),
                "processing_state": processing_state,
            },
        )
        return {"ok": True, "media_id": media_id, "mime": resolved_mime, "bytes": len(data)}

    @server.tool(
        description="Delete a post by id. Only posts made by the bound account can be deleted.",
        annotations=DESTRUCTIVE,
    )
    @_guarded
    async def delete_post(post_id: str, caller: str | None = None) -> dict[str, Any]:
        if not post_id or not str(post_id).strip():
            raise PulsarError("invalid_text", "post_id is required")
        deleted = await rt.client.delete_post(str(post_id).strip())
        rt.log.append(tool="delete_post", caller=caller, post_id=str(post_id))
        return {"ok": True, "post_id": str(post_id), "deleted": deleted}

    return server
