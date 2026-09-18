"""OAuth 2.0 authorization-code + PKCE login flow, run by a human on the host.

``pulsar auth login`` opens the X consent page in a browser, catches the
redirect on a loopback listener, exchanges the code, and hands the bundle to
the encrypted store. The MCP server never runs this.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import (
    CALLBACK_HOST,
    CALLBACK_PATH,
    CALLBACK_PORT,
    SCOPES,
    X_AUTHORIZE_URL,
    X_TOKEN_URL,
    Paths,
    callback_url,
)
from .errors import API_ERROR, PulsarError
from .store import TokenBundle, TokenStore


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
            API_ERROR, f"token exchange failed (HTTP {resp.status_code})", detail=resp.text[:500]
        )
    return TokenBundle.from_token_response(resp.json(), client_id=client_id)


class _Callback(BaseHTTPRequestHandler):
    result: dict[str, list[str]] | None = None
    expected_state: str = ""

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        parsed = urlparse(self.path)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        query = parse_qs(parsed.query)
        ok = query.get("state", [""])[0] == self.expected_state and "code" in query
        type(self).result = query
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"pulsar: authorization received, you can close this tab."
            if ok
            else b"pulsar: authorization failed or state mismatch; check the terminal."
        )

    def log_message(self, *_: object) -> None:  # keep the terminal quiet
        return


def wait_for_callback(state: str, *, timeout: float = 300.0) -> dict[str, list[str]]:
    _Callback.result = None
    _Callback.expected_state = state
    server = HTTPServer((CALLBACK_HOST, CALLBACK_PORT), _Callback)
    server.timeout = 1.0
    deadline = time.time() + timeout
    try:
        while _Callback.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if _Callback.result is None:
        raise PulsarError(API_ERROR, "timed out waiting for the browser redirect")
    return _Callback.result


def save_client_id(paths: Paths, client_id: str) -> None:
    paths.ensure()
    paths.client_file.write_text(
        json.dumps({"client_id": client_id, "redirect_uri": callback_url()}) + "\n"
    )


def load_client_id(paths: Paths) -> str | None:
    if not paths.client_file.exists():
        return None
    return json.loads(paths.client_file.read_text()).get("client_id")


def login(
    paths: Paths,
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> TokenBundle:
    pkce = Pkce.generate()
    url = build_authorize_url(client_id, pkce)
    print(f"Open this URL and approve as the account that should post:\n\n  {url}\n")
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    query = wait_for_callback(pkce.state)
    if query.get("state", [""])[0] != pkce.state:
        raise PulsarError(API_ERROR, "OAuth state mismatch; aborting")
    if "error" in query:
        raise PulsarError(API_ERROR, f"X denied authorization: {query['error'][0]}")
    bundle = exchange_code(client_id, query["code"][0], pkce, transport=transport)
    bind(paths, client_id, bundle)
    return bundle


def bind(paths: Paths, client_id: str, bundle: TokenBundle) -> None:
    """Persist a freshly issued bundle as this host's binding.

    A new token may belong to a different account than the last one, so the
    cached identity must not outlive the token it described — otherwise
    ``whoami`` and ``auth status`` keep naming the old account while posts go
    to the new one.
    """
    TokenStore(paths).save(bundle)
    save_client_id(paths, client_id)
    if paths.whoami_cache.exists():
        paths.whoami_cache.unlink()
