"""OAuth 2.0 authorization-code + PKCE login flow, run by a human on the host.

``pulsar auth login --account x:<handle>`` opens the X consent page in a
browser, catches the redirect on a loopback listener, and exchanges the code.
Before anything is stored it asks ``GET /2/users/me`` with the new token:
a token for another account than the one named is refused
(``account_mismatch``) and never written, which is what stops the wrong
token being stored under the right name (2026-09-16). The MCP server never
runs this. The loopback listener is the shared one (``channels.loopback``).
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from pulsar.internal.errors import API_ERROR, PulsarError
from pulsar.internal.fs import obj

from ..contract import Identity
from ..credentials import TokenBundle
from ..loopback import WAIT_SECONDS, CallbackServer, notify_stderr, open_quietly
from .client import bounded_text
from .config import (
    SCOPES,
    X_API_BASE,
    X_AUTHORIZE_URL,
    X_TOKEN_URL,
    callback_url,
)

PROVIDER = "x"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@dataclass(frozen=True)
class Pkce:
    verifier: str
    challenge: str
    state: str

    @classmethod
    def generate(cls) -> Pkce:
        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        return cls(verifier=verifier, challenge=challenge, state=secrets.token_urlsafe(24))


def build_authorize_url(client_id: str, pkce: Pkce, *, redirect_uri: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri or callback_url(),
        "scope": " ".join(SCOPES),
        "state": pkce.state,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
    }
    return f"{X_AUTHORIZE_URL}?{urlencode(params)}"


def exchange_code(
    client_id: str,
    code: str,
    pkce: Pkce,
    *,
    redirect_uri: str | None = None,
    transport: httpx.BaseTransport | None = None,
    token_url: str = X_TOKEN_URL,
) -> TokenBundle:
    with httpx.Client(transport=transport, timeout=30.0) as http:
        resp = http.post(
            token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri or callback_url(),
                "code_verifier": pkce.verifier,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code >= 400:
        raise PulsarError(
            API_ERROR,
            f"token exchange failed (HTTP {resp.status_code})",
            detail=bounded_text(resp.text),
        )
    return TokenBundle.from_token_response(resp.json(), client_id=client_id)


def wait_for_callback(state: str, *, timeout: float = 300.0) -> dict[str, list[str]]:
    server = CallbackServer(state)
    try:
        return server.wait(timeout)
    finally:
        server.server_close()


def authorize(
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
    notify: Callable[[str], None] = notify_stderr,
) -> TokenBundle:
    """The browser half: consent, callback, code exchange. Stores nothing."""
    pkce = Pkce.generate()
    url = build_authorize_url(client_id, pkce)
    server = CallbackServer(pkce.state)  # listening before the human can be redirected
    try:
        notify(f"Open this URL and approve as the account that should post:\n\n  {url}\n")
        if open_browser:
            threading.Thread(target=open_quietly, args=(url,), daemon=True).start()
        query = server.wait(WAIT_SECONDS)
    finally:
        server.server_close()
    if "error" in query:
        reason = bounded_text(query["error"][0])
        raise PulsarError(API_ERROR, f"X denied authorization: {reason}")
    if "code" not in query:
        raise PulsarError(API_ERROR, "the browser redirect carried no authorization code")
    return exchange_code(client_id, query["code"][0], pkce, transport=transport)


def fetch_identity(
    bundle: TokenBundle,
    *,
    transport: httpx.BaseTransport | None = None,
    base_url: str = X_API_BASE,
) -> Identity:
    """Who ``bundle``'s own access token belongs to (``GET /2/users/me``)."""
    with httpx.Client(transport=transport, timeout=30.0) as http:
        try:
            resp = http.get(
                f"{base_url.rstrip('/')}/users/me",
                headers={"Authorization": f"Bearer {bundle.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise PulsarError(
                API_ERROR,
                f"could not ask X who the new token belongs to ({exc.__class__.__name__}); "
                "nothing was stored",
            ) from exc
    if resp.status_code >= 400:
        raise PulsarError(
            API_ERROR,
            f"X refused GET /2/users/me with the new token (HTTP {resp.status_code}); "
            "nothing was stored",
        )
    try:
        data = obj(obj(resp.json()).get("data"))
        user_id, username = data["id"], data["username"]
    except (ValueError, KeyError) as exc:
        raise PulsarError(API_ERROR, "X answered /2/users/me without an id and username") from exc
    if not isinstance(username, str) or not username or user_id is None:
        raise PulsarError(API_ERROR, "X answered /2/users/me without an id and username")
    return Identity(provider_user_id=str(user_id), handle=username.lower())
