"""OAuth 2.0 authorization-code + PKCE login flow, run by a human on the host.

``pulsar auth login --account x:<handle>`` opens the X consent page in a
browser, catches the redirect on a loopback listener, and exchanges the code.
Before anything is stored it asks ``GET /2/users/me`` with the new token:
a token for another account than the one named is refused
(``account_mismatch``) and never written, which is what stops the wrong
token being stored under the right name (2026-09-16). The MCP server never
runs this.

The loopback listener is not trusted because it is loopback: a request counts
only with a ``Host`` naming the exact authority it bound (``127.0.0.1:<port>``
or ``localhost:<port>``), no ``Origin`` other than that same loopback origin,
and the ``state`` this login sent. Anything else is refused and recorded
nowhere, so another local page or process cannot end the wait or feed in a
code. The consent URL goes to ``notify`` (stderr by default), never to stdout.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from pulsar.internal.errors import API_ERROR, PulsarError
from pulsar.internal.fs import obj

from ..contract import Identity
from ..credentials import TokenBundle
from .client import bounded_text
from .config import (
    CALLBACK_HOST,
    CALLBACK_PATH,
    CALLBACK_PORT,
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


class CallbackServer(HTTPServer):
    """The loopback listener for one login: accepts only the redirect it is waiting for."""

    def __init__(self, state: str, *, host: str = CALLBACK_HOST, port: int = CALLBACK_PORT) -> None:
        super().__init__((host, port), _Callback)
        self.expected_state = state
        self.result: dict[str, list[str]] | None = None
        self.timeout = 1.0
        self.port: int = self.server_address[1]
        # The exact authorities the browser may name: what we bound, nothing wider.
        self.authorities = frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    def wait(self, timeout: float) -> dict[str, list[str]]:
        """Serve until a request with the right ``state`` arrives; ``api_error`` on timeout."""
        deadline = time.monotonic() + timeout
        while self.result is None and time.monotonic() < deadline:
            self.handle_request()
        if self.result is None:
            raise PulsarError(API_ERROR, "timed out waiting for the browser redirect")
        return self.result


class _Callback(BaseHTTPRequestHandler):
    server: CallbackServer  # pyright: ignore[reportIncompatibleVariableOverride]
    # A connection that sends nothing (a browser's preconnect) is dropped after
    # this long, so it cannot hold ``wait`` past its deadline.
    timeout = 5.0

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            self._reply(400, b"pulsar: missing Host header.")
            return
        if host not in self.server.authorities:
            self._reply(421, b"pulsar: this listener only answers to its own loopback address.")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin.strip().lower() != f"http://{host}":
            self._reply(403, b"pulsar: cross-origin requests are refused.")
            return
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self._reply(404, b"pulsar: not found.")
            return
        query = parse_qs(parsed.query)
        if not secrets.compare_digest(query.get("state", [""])[0], self.server.expected_state):
            # Not our redirect: refuse it and keep waiting for the real one.
            self._reply(400, b"pulsar: state mismatch; this is not the login in progress.")
            return
        self.server.result = query
        if "code" in query:
            self._reply(200, b"pulsar: authorization received, you can close this tab.")
        else:
            self._reply(400, b"pulsar: authorization failed; check the terminal.")

    def log_message(self, format: str, *args: object) -> None:  # keep the terminal quiet
        return


def wait_for_callback(state: str, *, timeout: float = 300.0) -> dict[str, list[str]]:
    server = CallbackServer(state)
    try:
        return server.wait(timeout)
    finally:
        server.server_close()


def notify_stderr(message: str) -> None:
    """The default ``notify``: the human reads the consent URL on stderr."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


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
        query = server.wait(300.0)
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


def open_quietly(url: str) -> None:
    """``webbrowser.open`` with file descriptor 1 on ``/dev/null``.

    A launched browser inherits stdout, and some launchers print to it
    ("Opening in existing browser session."), which would land before the
    command's JSON. Login writes nothing to stdout until the
    human has approved, so the brief swap cannot hide pulsar's own output.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        webbrowser.open(url)
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        os.close(devnull)
