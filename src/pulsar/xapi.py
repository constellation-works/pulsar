"""Thin async client over the X v2 API with token refresh baked in.

The transport is injectable so tests run against ``httpx.MockTransport``
and never touch the network.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import X_API_BASE, X_TOKEN_URL
from .errors import (
    API_ERROR,
    DUPLICATE,
    FORBIDDEN,
    INVALID_MEDIA,
    NOT_FOUND,
    RATE_LIMITED,
    AuthExpired,
    PulsarError,
)
from .store import TokenBundle, TokenStore

REFRESH_AHEAD_SECONDS = 120


def _error_detail(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:500]
    if isinstance(body, dict):
        # X returns either {"title","detail","type"} or {"errors":[...]}
        return {
            k: body[k] for k in ("title", "detail", "type", "errors", "reason") if k in body
        } or body
    return body


def _detail_text(detail: Any) -> str:
    if isinstance(detail, dict):
        parts = [str(detail.get(k, "")) for k in ("title", "detail", "reason")]
        for err in detail.get("errors") or []:
            if isinstance(err, dict):
                parts.append(str(err.get("message", "")))
        return " ".join(p for p in parts if p)
    return str(detail)


def map_http_error(resp: httpx.Response) -> PulsarError:
    detail = _error_detail(resp)
    text = _detail_text(detail).lower()
    if resp.status_code == 429:
        return PulsarError(RATE_LIMITED, "X rate limit hit; retry later", detail=detail)
    if resp.status_code == 404:
        return PulsarError(NOT_FOUND, "X could not find that resource", detail=detail)
    if resp.status_code == 403:
        if "duplicate" in text:
            return PulsarError(DUPLICATE, "X rejected the post as a duplicate", detail=detail)
        return PulsarError(FORBIDDEN, "X refused the request (forbidden)", detail=detail)
    if resp.status_code == 400 and "duplicate" in text:
        return PulsarError(DUPLICATE, "X rejected the post as a duplicate", detail=detail)
    return PulsarError(API_ERROR, f"X API error (HTTP {resp.status_code})", detail=detail)


class XClient:
    def __init__(
        self,
        store: TokenStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = X_API_BASE,
        token_url: str = X_TOKEN_URL,
        now: Callable[[], float] = time.time,
        timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._now = now
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._refresh_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth ---------------------------------------------------------------

    async def _bundle(self) -> TokenBundle:
        bundle = self.store.load()
        if bundle is None:
            raise AuthExpired("no X authorization on this host; run `pulsar auth login`")
        return bundle

    async def access_token(self) -> str:
        bundle = await self._bundle()
        if self._now() + REFRESH_AHEAD_SECONDS >= bundle.expires_at:
            bundle = await self.refresh(bundle)
        return bundle.access_token

    async def refresh(self, bundle: TokenBundle) -> TokenBundle:
        async with self._refresh_lock:
            current = self.store.load() or bundle
            if (
                current.access_token != bundle.access_token
                and self._now() + REFRESH_AHEAD_SECONDS < current.expires_at
            ):
                return current  # someone else refreshed while we waited
            if not current.refresh_token:
                raise AuthExpired("no refresh token stored; run `pulsar auth login`")
            try:
                resp = await self._http.post(
                    self.token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": current.refresh_token,
                        "client_id": current.client_id,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise PulsarError(
                    API_ERROR, f"token refresh failed: {exc.__class__.__name__}"
                ) from exc
            if resp.status_code in (400, 401, 403):
                raise AuthExpired()
            if resp.status_code >= 400:
                raise map_http_error(resp)
            fresh = TokenBundle.from_token_response(
                resp.json(), client_id=current.client_id, now=self._now()
            )
            if fresh.refresh_token is None:
                fresh.refresh_token = current.refresh_token
            self.store.save(fresh)
            return fresh

    # -- transport ----------------------------------------------------------

    async def request(
        self, method: str, path: str, *, _retry: bool = True, **kwargs: Any
    ) -> httpx.Response:
        token = await self.access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise PulsarError(API_ERROR, f"X request failed: {exc.__class__.__name__}") from exc
        if resp.status_code == 401 and _retry:
            bundle = await self._bundle()
            await self.refresh(bundle)
            return await self.request(method, path, _retry=False, headers=headers, **kwargs)
        if resp.status_code == 401:
            raise AuthExpired()
        if resp.status_code >= 400:
            raise map_http_error(resp)
        return resp

    # -- endpoints ----------------------------------------------------------

    async def me(self) -> dict[str, str]:
        data = (await self.request("GET", "/users/me")).json()["data"]
        return {"user_id": str(data["id"]), "username": data["username"]}

    async def create_post(
        self,
        text: str,
        *,
        reply_to_post_id: str | None = None,
        quote_post_id: str | None = None,
        media_ids: list[str] | None = None,
    ) -> dict[str, str]:
        body: dict[str, Any] = {"text": text}
        if reply_to_post_id:
            body["reply"] = {"in_reply_to_tweet_id": str(reply_to_post_id)}
        if quote_post_id:
            body["quote_tweet_id"] = str(quote_post_id)
        if media_ids:
            body["media"] = {"media_ids": [str(m) for m in media_ids]}
        data = (await self.request("POST", "/tweets", json=body)).json()["data"]
        return {"post_id": str(data["id"]), "text": data.get("text", text)}

    async def delete_post(self, post_id: str) -> bool:
        data = (await self.request("DELETE", f"/tweets/{post_id}")).json()
        return bool(data.get("data", {}).get("deleted", False))

    async def upload_image(self, data: bytes, mime: str, *, chunk_size: int = 1024 * 1024) -> str:
        """v2 chunked upload: initialize → append segments → finalize."""
        init = await self.request(
            "POST",
            "/media/upload/initialize",
            json={"media_type": mime, "total_bytes": len(data), "media_category": "tweet_image"},
        )
        media_id = str(init.json()["data"]["id"])
        for index, start in enumerate(range(0, len(data), chunk_size)):
            chunk = data[start : start + chunk_size]
            await self.request(
                "POST",
                f"/media/upload/{media_id}/append",
                data={"segment_index": str(index)},
                files={"media": (f"segment-{index}", chunk, mime)},
            )
        fin = (await self.request("POST", f"/media/upload/{media_id}/finalize")).json()
        state = (fin.get("data") or {}).get("processing_info", {}).get("state")
        if state and state not in ("succeeded", "pending", "in_progress"):
            raise PulsarError(INVALID_MEDIA, f"X media processing state: {state}", detail=fin)
        return media_id
