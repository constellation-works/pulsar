"""The MCP surface: exactly four tools, no secret parameters, structured errors.

Every tool returns a JSON object. Success carries ``ok: true``; failure
carries ``ok: false`` plus a machine-readable ``code`` from ``errors.py``
so the calling agent can branch (re-auth, retry later, rewrite text)
instead of parsing prose.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__
from .config import IMAGE_MIME_TYPES, MAX_IMAGE_BYTES, Paths, default_paths
from .errors import INVALID_MEDIA, PulsarError
from .guard import validate_text
from .store import TokenStore
from .writelog import WriteLog
from .xapi import XClient

TOOL_NAMES = ("whoami", "create_post", "upload_media", "delete_post")

INSTRUCTIONS = (
    "pulsar posts to X as the account a human authorized on this host. "
    "Call create_post only on explicit user intent in the current conversation "
    "or from a standing routine the owner enabled. Never pass credentials; there "
    "is no parameter for them. Use dry_run=true to validate first."
)


class Runtime:
    """Everything the tools need, built once per process (or per test)."""

    def __init__(
        self,
        paths: Paths | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        **client_kwargs: Any,
    ) -> None:
        self.paths = paths or default_paths()
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


def _load_media(path: str | None, base64_data: str | None, mime: str | None) -> tuple[bytes, str]:
    if bool(path) == bool(base64_data):
        raise PulsarError(INVALID_MEDIA, "pass exactly one of `path` or `base64`")
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise PulsarError(INVALID_MEDIA, f"no such file: {path}")
        data = p.read_bytes()
        mime = mime or mimetypes.guess_type(p.name)[0]
    else:
        try:
            data = base64.b64decode(base64_data or "", validate=True)
        except ValueError as exc:
            raise PulsarError(INVALID_MEDIA, "base64 payload is not valid") from exc
    if not mime:
        raise PulsarError(
            INVALID_MEDIA, "mime is required when the type cannot be guessed from the path"
        )
    if mime not in IMAGE_MIME_TYPES:
        raise PulsarError(
            INVALID_MEDIA, f"unsupported media type {mime}; v1 accepts {sorted(IMAGE_MIME_TYPES)}"
        )
    if not data:
        raise PulsarError(INVALID_MEDIA, "media is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise PulsarError(INVALID_MEDIA, f"media is {len(data)} bytes; limit is {MAX_IMAGE_BYTES}")
    return data, mime


def build_server(runtime: Runtime | None = None) -> MCPServer:
    rt = runtime or Runtime()
    server = MCPServer(name="pulsar", version=__version__, instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "Return the X account this connector is bound to: {user_id, username}. "
            "Local after the first call."
        )
    )
    @_guarded
    async def whoami() -> dict[str, Any]:
        me = await rt.whoami()
        return {"ok": True, **me}

    @server.tool(
        description=(
            "Create a post on X as the bound account. Requires explicit user intent in the "
            "calling chat or an owner-enabled routine. Text is validated (<=280 weighted chars, "
            "no credential-looking strings) before any network call. Set dry_run=true to validate "
            "only and get an estimated_cost_usd."
        )
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
        report = validate_text(text)
        if reply_to_post_id and quote_post_id:
            raise PulsarError("invalid_text", "a post cannot be both a reply and a quote in v1")
        if dry_run:
            rt.log.append(tool="create_post", caller=caller, text=text, dry_run=True)
            return {
                "ok": True,
                "dry_run": True,
                "text": text,
                "weighted_length": report.weighted_length,
                "has_url": report.has_url,
                "estimated_cost_usd": report.estimated_cost_usd,
                "pricing_note": "rough X credit pricing; verify live",
            }
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
            "Upload an image (png/jpeg/gif/webp, <=5MB) for a later create_post. "
            "Pass `path` or `base64`+`mime`. Returns {media_id}."
        )
    )
    @_guarded
    async def upload_media(
        path: str | None = None,
        base64: str | None = None,
        mime: str | None = None,
        caller: str | None = None,
    ) -> dict[str, Any]:
        data, resolved_mime = _load_media(path, base64, mime)
        media_id = await rt.client.upload_image(data, resolved_mime)
        rt.log.append(
            tool="upload_media",
            caller=caller,
            extra={"media_id": media_id, "mime": resolved_mime, "bytes": len(data)},
        )
        return {"ok": True, "media_id": media_id, "mime": resolved_mime, "bytes": len(data)}

    @server.tool(
        description="Delete a post by id. Only posts made by the bound account can be deleted."
    )
    @_guarded
    async def delete_post(post_id: str, caller: str | None = None) -> dict[str, Any]:
        if not post_id or not str(post_id).strip():
            raise PulsarError("invalid_text", "post_id is required")
        deleted = await rt.client.delete_post(str(post_id).strip())
        rt.log.append(tool="delete_post", caller=caller, post_id=str(post_id))
        return {"ok": True, "post_id": str(post_id), "deleted": deleted}

    return server
