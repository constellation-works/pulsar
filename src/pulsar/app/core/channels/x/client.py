"""Thin async client over the X v2 API with token refresh baked in.

The transport is injectable so tests run against ``httpx.MockTransport``
and never touch the network. Store reads and writes (Fernet, fsync) run in a
worker thread so a refresh never blocks the event loop; the per-client
``asyncio.Lock`` and the store's cross-process lock keep one refresh in
flight per account. Text X sends back is embedded in error messages only
through ``bounded_text``.

A write is sent with the token of the login whose identity was checked.
``pinned(binding_id)`` is this client (same connection pool, same refresh
lock) refusing to send under any other login: every request re-reads the
stored bundle, and a bundle a re-login replaced after the check is
``account_mismatch`` before a byte of the request leaves (after a logout,
``auth_expired``). A refresh keeps the binding, so a refreshed token still
sends.
"""

from __future__ import annotations

import asyncio
import copy
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from pulsar.internal.errors import (
    ACCOUNT_MISMATCH,
    API_ERROR,
    DUPLICATE,
    FORBIDDEN,
    INTERNAL,
    INVALID_ARGUMENT,
    INVALID_MEDIA,
    NOT_FOUND,
    RATE_LIMITED,
    UPLOAD_TIMEOUT,
    AuthExpired,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.internal.fs import as_list, as_object, obj

from ..credentials import (
    REFRESH_LOCK_WAIT_SECONDS,
    CredentialConflict,
    CredentialStore,
    TokenBundle,
)
from .config import X_API_BASE, X_TOKEN_URL

# Raised before any request byte reaches X: no connection, no pool slot, or a
# request httpx refused to build. Retrying one of these cannot double-write.
# Everything else (read/write timeouts, dropped connections, protocol errors
# mid-response) may have happened after X received the request.
NOT_SENT_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)

REFRESH_AHEAD_SECONDS = 120
IMAGE_CHUNK_BYTES = 1024 * 1024
VIDEO_CHUNK_BYTES = 4 * 1024 * 1024
PROCESSING_TIMEOUT_SECONDS = 300
UPLOAD_DEADLINE_FLOOR_SECONDS = 5
UPLOAD_MIN_BYTES_PER_SECOND = 5 * 1024 * 1024
# The most provider-supplied text an error message embeds.
PROVIDER_TEXT_LIMIT = 500


_X_ID = re.compile(r"[0-9]{1,19}")


def bounded_text(value: object, limit: int = PROVIDER_TEXT_LIMIT) -> str:
    """``value`` as text of at most ``limit`` characters, visibly marked when cut."""
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [truncated {len(text) - limit} of {len(text)} characters]"


def check_x_id(value: object, field: str) -> str:
    """An X snowflake id (post, user, media) as a string, or ``invalid_argument``.

    Ids are interpolated into request paths, so anything but digits could
    steer a call to another endpoint (``../users/1/retweets/2``) or smuggle a
    query string.
    """
    text = str(value).strip() if value is not None else ""
    if not _X_ID.fullmatch(text):
        raise PulsarError(
            INVALID_ARGUMENT, f"{field} must be a numeric X id (1-19 digits)", detail={field: text}
        )
    return text


class MediaProcessingError(PulsarError):
    def __init__(self, state: str, message: str, *, detail: Any) -> None:
        super().__init__(INVALID_MEDIA, message, detail=detail)
        self.processing_state = state


def _error_detail(resp: httpx.Response) -> Any:
    try:
        body = resp.json()
    except ValueError:
        return bounded_text(resp.text)
    if (fields := as_object(body)) is not None:
        # X returns either {"title","detail","type"} or {"errors":[...]}
        return {
            k: fields[k] for k in ("title", "detail", "type", "errors", "reason") if k in fields
        } or fields
    return body


def _detail_text(detail: Any) -> str:
    if (fields := as_object(detail)) is not None:
        parts = [str(fields.get(k, "")) for k in ("title", "detail", "reason")]
        for err in as_list(fields.get("errors")):
            if (entry := as_object(err)) is not None:
                parts.append(str(entry.get("message", "")))
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
        store: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = X_API_BASE,
        token_url: str = X_TOKEN_URL,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        timeout: float = 30.0,
    ) -> None:
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._refresh_lock = asyncio.Lock()
        self._pinned = False
        self._binding: str | None = None

    def pinned(self, binding_id: str | None) -> XClient:
        """This client, sending only with a bundle of the login ``binding_id`` names.

        Shares this client's connection pool and refresh lock; the unpinned
        client owns them, and closing it closes the view too.
        """
        view = copy.copy(self)
        view._pinned, view._binding = True, binding_id
        return view

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- auth ---------------------------------------------------------------

    async def _load(self) -> TokenBundle | None:
        return await asyncio.to_thread(self.store.load)

    async def _bundle(self) -> TokenBundle:
        bundle = await self._load()
        if bundle is None:
            raise AuthExpired(
                f"no X authorization is stored for this account; {self.store.reauth_hint()}"
            )
        return bundle

    def _expired(self, what: str) -> AuthExpired:
        return AuthExpired(f"{what}; {self.store.reauth_hint()}")

    def _expiring(self, bundle: TokenBundle) -> bool:
        return self._now() + REFRESH_AHEAD_SECONDS >= bundle.expires_at

    def _held(self, bundle: TokenBundle) -> TokenBundle:
        """``bundle``, if a pinned client may send with it."""
        if self._pinned and bundle.binding_id != self._binding:
            raise PulsarError(
                ACCOUNT_MISMATCH,
                "the stored X authorization changed (a re-login) after this "
                "account's identity was checked; nothing was sent. Retry to check the new "
                "binding",
                retryable=True,
            )
        return bundle

    async def access_token(self) -> str:
        bundle = self._held(await self._bundle())
        if self._expiring(bundle):
            bundle = self._held(await self.refresh(bundle))
        return bundle.access_token

    async def refresh(self, bundle: TokenBundle) -> TokenBundle:
        """Replace ``bundle`` (expiring, or rejected by X) with a working one.

        X rotates the refresh token on every use, so a second refresh with the
        same token fails. The asyncio lock coalesces refreshes inside this
        process; the store's cross-process lock is held from the re-load to
        the save, so another process sharing this home either refreshes first
        (and we reuse its bundle) or waits for us.
        """
        async with self._refresh_lock:
            async with self.store.refresh_lock(REFRESH_LOCK_WAIT_SECONDS):
                try:
                    return await self._refresh_locked(bundle)
                except PulsarError:
                    raise
                except Exception as exc:
                    # A save that failed, or a bug. Nothing but the token POST
                    # was sent, so a write that got here did not happen: never
                    # let this surface as outcome_unknown.
                    raise PulsarError(
                        INTERNAL,
                        f"token refresh failed inside pulsar: {exc.__class__.__name__}. X may "
                        "have rotated the token pair already; if later calls return "
                        f"auth_expired, {self.store.reauth_hint()}",
                    ) from exc

    async def _refresh_locked(self, stale: TokenBundle) -> TokenBundle:
        current = await self._bundle()
        if current.access_token != stale.access_token and not self._expiring(current):
            return current  # another refresher (this process or another) got there first
        # Two attempts: the second only after a rejected refresh token turned out
        # to have been rotated by a process that ignored the lock.
        for _ in range(2):
            if not current.refresh_token:
                raise self._expired("no refresh token is stored for this account")
            resp = await self._post_refresh(current)
            if resp.status_code in (400, 401, 403):
                newer = await self._load()
                if newer is None or newer.refresh_token == current.refresh_token:
                    raise self._expired(
                        f"X refused the refresh token (HTTP {resp.status_code}): it was revoked "
                        "or has expired"
                    )
                if not self._expiring(newer):
                    return newer
                current = newer
                continue
            if resp.status_code >= 400:
                raise map_http_error(resp)
            try:
                fresh = TokenBundle.from_token_response(
                    resp.json(), client_id=current.client_id, now=self._now()
                )
            except (ValueError, KeyError, TypeError) as exc:
                # X answered 200 with something that is not a token pair.
                raise PulsarError(
                    API_ERROR,
                    f"token refresh failed: X's answer was unreadable ({exc.__class__.__name__}). "
                    f"If later calls return auth_expired, {self.store.reauth_hint()}",
                    retryable=True,
                ) from exc
            if fresh.refresh_token is None:
                fresh.refresh_token = current.refresh_token
            fresh.binding_id = current.binding_id
            try:
                await asyncio.to_thread(self.store.save, fresh, expected_previous=current)
            except CredentialConflict:
                # Login and logout hold the lock too, so this is a writer that
                # ignored it; the stored binding wins over our rotation.
                return await self._bundle()
            return fresh
        raise self._expired("X refused the refresh token twice")

    async def _post_refresh(self, bundle: TokenBundle) -> httpx.Response:
        try:
            return await self._http.post(
                self.token_url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": bundle.refresh_token,
                    "client_id": bundle.client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except NOT_SENT_ERRORS as exc:
            raise PulsarError(
                API_ERROR, f"token refresh was not sent: {exc.__class__.__name__}", retryable=True
            ) from exc
        except httpx.HTTPError as exc:
            # The POST may have reached X, which rotates the pair on use: say
            # so rather than report a plain failure.
            raise PulsarError(
                API_ERROR,
                f"token refresh reply lost ({exc.__class__.__name__}); X may have rotated the "
                f"token pair already. If later calls return auth_expired, "
                f"{self.store.reauth_hint()}",
                retryable=True,
                detail={"outcome": "unknown"},
            ) from exc

    # -- transport ----------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        non_idempotent: bool = False,
        _retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send one authenticated request, mapping failures to ``PulsarError``.

        ``non_idempotent`` marks a write that must not happen twice (``POST
        /tweets``). For those, any failure that leaves open whether X acted on
        the request — the connection broke after bytes left, or X answered
        5xx — raises ``OutcomeUnknown`` instead of a retryable ``api_error``,
        because a naive retry double-posts. Failures that prove nothing was
        sent stay ``api_error`` (retryable) either way; a 401 is not a post,
        so refresh-and-retry is still safe.
        """
        token = await self.access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        try:
            resp = await self._http.request(method, url, headers=headers, **kwargs)
        except NOT_SENT_ERRORS as exc:
            raise PulsarError(
                API_ERROR,
                f"X request was not sent: {exc.__class__.__name__}",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            name = exc.__class__.__name__
            if non_idempotent:
                raise OutcomeUnknown(f"{name} after the request may have reached X") from exc
            raise PulsarError(API_ERROR, f"X request failed: {name}", retryable=True) from exc
        if resp.status_code == 401 and _retry:
            current = self._held(await self._bundle())
            if current.access_token == token:
                await self.refresh(current)
            # Otherwise another process rotated since we read the token: retry with
            # its bundle rather than burn a second rotation of the refresh token.
            return await self.request(
                method,
                path,
                non_idempotent=non_idempotent,
                _retry=False,
                headers=headers,
                **kwargs,
            )
        if resp.status_code == 401:
            raise self._expired("X refused the access token again right after a refresh")
        if resp.status_code >= 500 and non_idempotent:
            raise OutcomeUnknown(
                f"X answered HTTP {resp.status_code}",
                detail={"status": resp.status_code, "x": _error_detail(resp)},
            )
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
        resp = await self.request("POST", "/tweets", json=body, non_idempotent=True)
        try:
            data = resp.json()["data"]
            return {"post_id": str(data["id"]), "text": data.get("text", text)}
        except (ValueError, KeyError, TypeError) as exc:
            # X said yes but we cannot tell which post it made.
            raise OutcomeUnknown(
                f"X answered HTTP {resp.status_code} without a readable post id",
                detail={"status": resp.status_code},
            ) from exc

    async def delete_post(self, post_id: str) -> bool:
        post_id = check_x_id(post_id, "post_id")
        data = (await self.request("DELETE", f"/tweets/{post_id}")).json()
        return bool(data.get("data", {}).get("deleted", False))

    async def upload_media(
        self, data: bytes, mime: str, *, chunk_size: int | None = None
    ) -> tuple[str, str]:
        """Upload media and return its ID and processing state."""
        is_video = mime == "video/mp4"
        category = "tweet_video" if is_video else "tweet_image"
        chunk_size = chunk_size or (VIDEO_CHUNK_BYTES if is_video else IMAGE_CHUNK_BYTES)
        deadline = (
            self._monotonic()
            + UPLOAD_DEADLINE_FLOOR_SECONDS
            + len(data) / UPLOAD_MIN_BYTES_PER_SECOND
        )

        async def upload_request(method: str, path: str, **kwargs: Any) -> httpx.Response:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise PulsarError(UPLOAD_TIMEOUT, "X media upload timed out")
            try:
                response = await asyncio.wait_for(
                    self.request(method, path, **kwargs), timeout=remaining
                )
            except TimeoutError as exc:
                raise PulsarError(UPLOAD_TIMEOUT, "X media upload timed out") from exc
            if self._monotonic() >= deadline:
                raise PulsarError(UPLOAD_TIMEOUT, "X media upload timed out")
            return response

        init = await upload_request(
            "POST",
            "/media/upload/initialize",
            json={"media_type": mime, "total_bytes": len(data), "media_category": category},
        )
        try:
            media_id = check_x_id(init.json()["data"]["id"], "media_id")
        except (ValueError, KeyError, TypeError, PulsarError) as exc:
            raise PulsarError(API_ERROR, "X media init returned no usable media id") from exc
        for index, start in enumerate(range(0, len(data), chunk_size)):
            chunk = data[start : start + chunk_size]
            await upload_request(
                "POST",
                f"/media/upload/{media_id}/append",
                data={"segment_index": str(index)},
                files={"media": (f"segment-{index}", chunk, mime)},
            )
        fin = obj((await upload_request("POST", f"/media/upload/{media_id}/finalize")).json())
        info = as_object(obj(fin.get("data")).get("processing_info"))
        if is_video and info is not None:
            state = await self._wait_for_processing(media_id, info)
        else:
            state = str(obj(info).get("state", "succeeded"))
            if state not in ("succeeded", "pending", "in_progress"):
                raise MediaProcessingError(
                    state, f"X media processing state: {bounded_text(state)}", detail=info
                )
        return media_id, state

    async def _wait_for_processing(self, media_id: str, info: dict[str, Any]) -> str:
        deadline = self._monotonic() + PROCESSING_TIMEOUT_SECONDS
        while True:
            state = str(info.get("state"))
            if state == "succeeded":
                return state
            if state == "failed":
                error = info.get("error") or info
                fields = as_object(error)
                message = fields.get("message") if fields is not None else str(error)
                raise MediaProcessingError(
                    state,
                    f"X media processing failed: {bounded_text(message or 'no detail from X')}",
                    detail=info,
                )
            if state not in ("pending", "in_progress"):
                raise MediaProcessingError(
                    "unknown", f"X media processing state: {bounded_text(state)}", detail=info
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise MediaProcessingError("timed_out", "X media processing timed out", detail=info)
            check_after = info.get("check_after_secs")
            delay = check_after if isinstance(check_after, (int, float)) and check_after > 0 else 1
            await self._sleep(min(delay, remaining))
            if self._monotonic() >= deadline:
                raise MediaProcessingError("timed_out", "X media processing timed out", detail=info)
            status = await self.request(
                "GET", "/media/upload", params={"command": "STATUS", "media_id": media_id}
            )
            body = obj(status.json())
            next_info = as_object(obj(body.get("data")).get("processing_info"))
            if next_info is None:
                raise MediaProcessingError(
                    "unknown", "X media status has no processing_info", detail=body
                )
            info = next_info
