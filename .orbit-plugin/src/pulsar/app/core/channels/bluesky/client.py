"""Thin async XRPC client over one account's PDS, with token refresh baked in.

The transport is injectable so tests run against ``httpx.MockTransport`` and
never touch the network. It keeps ``XClient``'s guarantees: store reads and
writes run in a worker thread; a per-client ``asyncio.Lock`` and the store's
cross-process lock keep one refresh in flight per account, saved by
compare-and-swap; ``pinned(binding_id)`` refuses to send under any login but
the one whose identity was checked (``account_mismatch``).

Authorization. A bundle whose ``token_type`` is ``DPoP`` (atproto OAuth) is
sent as ``Authorization: DPoP <token>`` with a ``DPoP`` proof signed per
request with the key stored in the bundle (or an injected ``DpopProof``); the
client keeps each server's latest ``DPoP-Nonce`` and repeats a request once
when the server asks for a fresh one (it refused the request, so the repeat
cannot double-write). A DPoP-bound bundle without a key is refused before
anything is sent; any other bundle is sent as a bearer token. A bundle that
names its PDS (``service``) and token endpoint (``token_url``), as a login's
does, is sent there; the constructor's are the defaults for one that does not.

Failures follow the channel contract: a request that provably never left is
retryable ``api_error``; for a ``non_idempotent`` procedure
(``createRecord``) a dropped connection after sending, a 5xx or an unreadable
success is ``outcome_unknown``. An expired token (401, or 400
``ExpiredToken``) is refreshed once and the request repeated.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from pulsar.internal.errors import (
    ACCOUNT_MISMATCH,
    API_ERROR,
    FORBIDDEN,
    INTERNAL,
    INVALID_MEDIA,
    NOT_FOUND,
    RATE_LIMITED,
    UNSUPPORTED,
    UPLOAD_TIMEOUT,
    AuthExpired,
    OutcomeUnknown,
    PulsarError,
)
from pulsar.internal.fs import as_object, obj
from pulsar.internal.guard import redact

from ..credentials import (
    REFRESH_LOCK_WAIT_SECONDS,
    CredentialConflict,
    CredentialStore,
    TokenBundle,
)
from .config import BSKY_SERVICE, BSKY_TOKEN_URL
from .dpop import Es256Proof
from .interfaces import DpopProof

# Raised before any request byte reaches the server; retrying cannot double-write.
NOT_SENT_ERRORS: tuple[type[httpx.HTTPError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
)

REFRESH_AHEAD_SECONDS = 120
UPLOAD_DEADLINE_FLOOR_SECONDS = 30
UPLOAD_MIN_BYTES_PER_SECOND = 256 * 1024
# The most provider-supplied text an error embeds.
PROVIDER_TEXT_LIMIT = 500
# 400 errors that mean the access token, not the request, is bad.
_TOKEN_ERRORS = frozenset({"ExpiredToken", "InvalidToken"})
_NOT_FOUND_ERRORS = frozenset({"RecordNotFound", "NotFound", "RepoNotFound"})
_MEDIA_ERRORS = frozenset({"BlobTooLarge", "PayloadTooLarge", "InvalidMimeType"})


def _bounded(value: object) -> str:
    text = redact(str(value))
    if len(text) <= PROVIDER_TEXT_LIMIT:
        return text
    return f"{text[:PROVIDER_TEXT_LIMIT]}… [truncated]"


def error_detail(resp: httpx.Response) -> dict[str, Any]:
    """XRPC's ``{error, message}``, bounded and redacted; the status for the rest."""
    try:
        body = as_object(resp.json())
    except ValueError:
        body = None
    detail: dict[str, Any] = {"status": resp.status_code}
    for key in ("error", "message", "error_description"):
        value = body.get(key) if body is not None else None
        if isinstance(value, str) and value:
            detail[key] = _bounded(value)
    return detail


def map_http_error(resp: httpx.Response) -> PulsarError:
    detail = error_detail(resp)
    name = detail.get("error")
    if resp.status_code == 429 or name == "RateLimitExceeded":
        return PulsarError(RATE_LIMITED, "Bluesky rate limit hit; retry later", detail=detail)
    if resp.status_code == 404 or name in _NOT_FOUND_ERRORS:
        return PulsarError(NOT_FOUND, "Bluesky could not find that record", detail=detail)
    if resp.status_code == 413 or name in _MEDIA_ERRORS:
        return PulsarError(INVALID_MEDIA, "Bluesky refused the media", detail=detail)
    if resp.status_code == 403:
        return PulsarError(FORBIDDEN, "Bluesky refused the request (forbidden)", detail=detail)
    return PulsarError(API_ERROR, f"Bluesky API error (HTTP {resp.status_code})", detail=detail)


def htu(url: str) -> str:
    """``url`` without query and fragment: what a DPoP proof names."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _error_name(resp: httpx.Response) -> str | None:
    try:
        body = as_object(resp.json())
    except ValueError:
        return None
    name = body.get("error") if body is not None else None
    return name if isinstance(name, str) else None


def wants_nonce(resp: httpx.Response) -> bool:
    """The server refused the request for want of a (fresh) DPoP nonce, and sent one."""
    if resp.status_code not in (400, 401) or not resp.headers.get("DPoP-Nonce"):
        return False
    challenge = resp.headers.get("WWW-Authenticate", "")
    return "use_dpop_nonce" in challenge or _error_name(resp) == "use_dpop_nonce"


def _token_rejected(resp: httpx.Response) -> bool:
    return resp.status_code == 401 or (
        resp.status_code == 400 and _error_name(resp) in _TOKEN_ERRORS
    )


class BlueskyClient:
    def __init__(
        self,
        store: CredentialStore,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        service: str = BSKY_SERVICE,
        token_url: str = BSKY_TOKEN_URL,
        proof: DpopProof | None = None,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        timeout: float = 30.0,
        upload_deadline: float | None = None,
    ) -> None:
        self.store = store
        self.service = service.rstrip("/")
        self.token_url = token_url
        self._proof = proof
        self._now = now
        self._monotonic = monotonic
        self._upload_deadline = upload_deadline
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout)
        self._refresh_lock = asyncio.Lock()
        self._nonces: dict[str, str] = {}
        self._signers: dict[str, DpopProof] = {}  # by stored key
        self._pinned = False
        self._binding: str | None = None

    def pinned(self, binding_id: str | None) -> BlueskyClient:
        """This client, sending only with a bundle of the login ``binding_id`` names.

        Shares this client's connection pool, refresh lock and nonces; the
        unpinned client owns them, and closing it closes the view too.
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
                f"no Bluesky authorization is stored for this account; {self.store.reauth_hint()}"
            )
        return bundle

    def _expired(self, what: str) -> AuthExpired:
        return AuthExpired(f"{what}; {self.store.reauth_hint()}")

    def _expiring(self, bundle: TokenBundle) -> bool:
        return self._now() + REFRESH_AHEAD_SECONDS >= bundle.expires_at

    def _held(self, bundle: TokenBundle) -> TokenBundle:
        if self._pinned and bundle.binding_id != self._binding:
            raise PulsarError(
                ACCOUNT_MISMATCH,
                "the stored Bluesky authorization changed (a re-login) after this account's "
                "identity was checked; nothing was sent. Retry to check the new binding",
                retryable=True,
            )
        return bundle

    async def _current(self) -> TokenBundle:
        bundle = self._held(await self._bundle())
        if self._expiring(bundle):
            bundle = self._held(await self.refresh(bundle))
        return bundle

    def _signer(self, bundle: TokenBundle) -> DpopProof | None:
        """What signs ``bundle``'s proofs: the injected one, else its stored key."""
        if self._proof is not None or bundle.dpop_key is None:
            return self._proof
        signer = self._signers.get(bundle.dpop_key)
        if signer is None:
            try:
                signer = Es256Proof.from_stored(bundle.dpop_key)
            except ValueError as exc:
                raise PulsarError(
                    UNSUPPORTED,
                    "the DPoP key stored with this Bluesky login is unusable and nothing was "
                    f"sent; {self.store.reauth_hint()}",
                ) from exc
            self._signers[bundle.dpop_key] = signer
        return signer

    def _dpop(
        self, signer: DpopProof | None, method: str, url: str, access_token: str | None
    ) -> dict[str, str]:
        if signer is None:
            return {}
        nonce = self._nonces.get(origin(url))
        return {"DPoP": signer(method, htu(url), nonce=nonce, access_token=access_token)}

    def _authorization(
        self, signer: DpopProof | None, method: str, url: str, bundle: TokenBundle
    ) -> dict[str, str]:
        if bundle.token_type.lower() != "dpop":
            return {"Authorization": f"Bearer {bundle.access_token}"}
        if signer is None:
            raise PulsarError(
                UNSUPPORTED,
                "this Bluesky login is DPoP-bound and pulsar was given no DPoP key to sign "
                "requests with; nothing was sent",
            )
        return {
            "Authorization": f"DPoP {bundle.access_token}",
            **self._dpop(signer, method, url, bundle.access_token),
        }

    def _remember_nonce(self, url: str, resp: httpx.Response) -> None:
        nonce = resp.headers.get("DPoP-Nonce")
        if nonce:
            self._nonces[origin(url)] = nonce

    async def refresh(self, bundle: TokenBundle) -> TokenBundle:
        """Replace ``bundle`` (expiring, or refused) with a working one.

        atproto's authorization servers rotate the refresh token on use, so
        the refresh holds the store's cross-process lock from the re-load to
        the save, as ``XClient.refresh`` does.
        """
        async with self._refresh_lock:
            async with self.store.refresh_lock(REFRESH_LOCK_WAIT_SECONDS):
                try:
                    return await self._refresh_locked(bundle)
                except PulsarError:
                    raise
                except Exception as exc:
                    raise PulsarError(
                        INTERNAL,
                        f"token refresh failed inside pulsar: {exc.__class__.__name__}. The "
                        "server may have rotated the token pair already; if later calls return "
                        f"auth_expired, {self.store.reauth_hint()}",
                    ) from exc

    async def _refresh_locked(self, stale: TokenBundle) -> TokenBundle:
        current = await self._bundle()
        if current.access_token != stale.access_token and not self._expiring(current):
            return current  # another refresher got there first
        for _ in range(2):
            if not current.refresh_token:
                raise self._expired("no refresh token is stored for this account")
            resp = await self._post_refresh(current)
            if resp.status_code in (400, 401, 403):
                newer = await self._load()
                if newer is None or newer.refresh_token == current.refresh_token:
                    raise self._expired(
                        f"Bluesky refused the refresh token (HTTP {resp.status_code}): it was "
                        "revoked or has expired"
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
                raise PulsarError(
                    API_ERROR,
                    "token refresh failed: the answer was unreadable "
                    f"({exc.__class__.__name__}). If later calls return auth_expired, "
                    f"{self.store.reauth_hint()}",
                    retryable=True,
                ) from exc
            if fresh.refresh_token is None:
                fresh.refresh_token = current.refresh_token
            fresh.binding_id = current.binding_id
            fresh.dpop_key, fresh.service = current.dpop_key, current.service
            fresh.token_url = current.token_url
            try:
                await asyncio.to_thread(self.store.save, fresh, expected_previous=current)
            except CredentialConflict:
                return await self._bundle()
            return fresh
        raise self._expired("Bluesky refused the refresh token twice")

    async def _post_refresh(self, bundle: TokenBundle) -> httpx.Response:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": bundle.refresh_token,
            "client_id": bundle.client_id,
        }
        token_url = bundle.token_url or self.token_url
        signer = self._signer(bundle)
        nonced = False  # repeated once, for a fresh DPoP nonce
        while True:
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                **self._dpop(signer, "POST", token_url, None),
            }
            try:
                resp = await self._http.post(token_url, data=form, headers=headers)
            except NOT_SENT_ERRORS as exc:
                raise PulsarError(
                    API_ERROR,
                    f"token refresh was not sent: {exc.__class__.__name__}",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                raise PulsarError(
                    API_ERROR,
                    f"token refresh reply lost ({exc.__class__.__name__}); the server may have "
                    "rotated the token pair already. If later calls return auth_expired, "
                    f"{self.store.reauth_hint()}",
                    retryable=True,
                    detail={"outcome": "unknown"},
                ) from exc
            self._remember_nonce(token_url, resp)
            if nonced or signer is None or not wants_nonce(resp):
                return resp
            nonced = True

    # -- transport ----------------------------------------------------------

    async def _send(
        self,
        method: str,
        nsid: str,
        *,
        non_idempotent: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        extra = dict(kwargs.pop("headers", {}) or {})
        refreshed = nonced = False
        while True:
            bundle = await self._current()
            url = f"{(bundle.service or self.service).rstrip('/')}/xrpc/{nsid}"
            signer = self._signer(bundle)
            headers = {**extra, **self._authorization(signer, method, url, bundle)}
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except NOT_SENT_ERRORS as exc:
                raise PulsarError(
                    API_ERROR,
                    f"Bluesky request was not sent: {exc.__class__.__name__}",
                    retryable=True,
                ) from exc
            except httpx.HTTPError as exc:
                name = exc.__class__.__name__
                if non_idempotent:
                    raise OutcomeUnknown(
                        f"{name} after the request may have reached Bluesky"
                    ) from exc
                raise PulsarError(
                    API_ERROR, f"Bluesky request failed: {name}", retryable=True
                ) from exc
            self._remember_nonce(url, resp)
            if not nonced and signer is not None and wants_nonce(resp):
                nonced = True
                continue
            if _token_rejected(resp):
                if refreshed:
                    raise self._expired(
                        "Bluesky refused the access token again right after a refresh"
                    )
                refreshed = True
                current = self._held(await self._bundle())
                if current.access_token == bundle.access_token:
                    await self.refresh(current)
                continue
            if resp.status_code >= 500 and non_idempotent:
                raise OutcomeUnknown(
                    f"Bluesky answered HTTP {resp.status_code}", detail=error_detail(resp)
                )
            if resp.status_code >= 400:
                raise map_http_error(resp)
            return resp

    @staticmethod
    def _json(resp: httpx.Response, nsid: str, *, non_idempotent: bool) -> dict[str, Any]:
        try:
            body = as_object(resp.json())
        except ValueError:
            body = None
        if body is not None:
            return body
        if non_idempotent:
            raise OutcomeUnknown(
                f"Bluesky answered {nsid} with HTTP {resp.status_code} and an unreadable body",
                detail={"status": resp.status_code},
            )
        raise PulsarError(API_ERROR, f"Bluesky {nsid} response is not a JSON object")

    async def query(self, nsid: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """``GET /xrpc/<nsid>``: a read."""
        resp = await self._send("GET", nsid, params=dict(params or {}))
        return self._json(resp, nsid, non_idempotent=False)

    async def procedure(
        self, nsid: str, body: Mapping[str, Any], *, non_idempotent: bool = False
    ) -> dict[str, Any]:
        """``POST /xrpc/<nsid>`` with a JSON body. ``non_idempotent`` marks a write
        that must not happen twice (``createRecord``)."""
        resp = await self._send("POST", nsid, json=dict(body), non_idempotent=non_idempotent)
        if not resp.content:
            return {}
        return self._json(resp, nsid, non_idempotent=non_idempotent)

    async def upload_blob(self, data: bytes, mime: str) -> dict[str, Any]:
        """Upload one blob; returns its ``blob`` ref. Creates nothing visible.

        Bounded like ``XClient.upload_media``: 30 seconds plus the bytes at
        256 KiB/s, or the caller's earlier ``upload_deadline``.
        """
        deadline = (
            self._monotonic()
            + UPLOAD_DEADLINE_FLOOR_SECONDS
            + len(data) / UPLOAD_MIN_BYTES_PER_SECOND
        )
        if self._upload_deadline is not None:
            deadline = min(deadline, self._upload_deadline)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise PulsarError(UPLOAD_TIMEOUT, "Bluesky blob upload timed out")
        upload: Awaitable[httpx.Response] = self._send(
            "POST", "com.atproto.repo.uploadBlob", content=data, headers={"Content-Type": mime}
        )
        try:
            resp = await asyncio.wait_for(upload, timeout=remaining)
        except TimeoutError as exc:
            raise PulsarError(UPLOAD_TIMEOUT, "Bluesky blob upload timed out") from exc
        blob = as_object(self._json(resp, "uploadBlob", non_idempotent=False).get("blob"))
        if blob is None or not isinstance(obj(blob.get("ref")).get("$link"), str):
            raise PulsarError(API_ERROR, "Bluesky uploadBlob returned no usable blob ref")
        return blob

    async def me(self) -> dict[str, str]:
        """``{user_id, username}``: the session's DID and handle."""
        session = await self.query("com.atproto.server.getSession")
        did, handle = session.get("did"), session.get("handle")
        if not isinstance(did, str) or not isinstance(handle, str):
            raise PulsarError(API_ERROR, "Bluesky getSession returned no did and handle")
        return {"user_id": did, "username": handle}
