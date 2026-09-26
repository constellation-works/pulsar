"""OAuth 2.0 authorization-code + PKCE login flow, run by a human on the host.

``pulsar auth login --account x:<handle>`` opens the X consent page in a
browser, catches the redirect on a loopback listener, and exchanges the code.
Before anything is stored it asks ``GET /2/users/me`` with the new token:
a token for another account than the one named is refused
(``account_mismatch``) and never written, which is what stops the wrong
token being stored under the right name (2026-09-16). The MCP server never
runs this.
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

from ...core.accounts import Account, AccountRegistry, canonical_alias, check_handle
from ...core.adapter import Identity
from ...core.errors import API_ERROR, INVALID_ARGUMENT, PulsarError
from ...core.fsutil import write_private_atomic
from ...core.jsonx import as_object, obj
from ...core.paths import Paths
from ...core.plan import alias_provider
from ...core.settings import Settings
from ...core.store import TokenBundle
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

    def log_message(self, format: str, *args: object) -> None:  # keep the terminal quiet
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


def _client_records(paths: Paths) -> dict[str, dict[str, str]]:
    """``client.json`` by provider. The phase 1 file was X's record at the top level."""
    try:
        data = obj(json.loads(paths.client_file.read_text()))
    except (FileNotFoundError, ValueError):
        return {}
    if "client_id" in data:  # phase 1: {"client_id", "redirect_uri"}
        return {PROVIDER: {k: str(v) for k, v in data.items()}}
    out: dict[str, dict[str, str]] = {}
    for provider, record in data.items():
        fields = as_object(record)
        if fields is not None:
            out[provider] = {k: str(v) for k, v in fields.items()}
    return out


def save_client_id(paths: Paths, client_id: str) -> None:
    """Remember X's OAuth client id: one per provider, shared by every account."""
    paths.ensure()
    records = _client_records(paths)
    records[PROVIDER] = {"client_id": client_id, "redirect_uri": callback_url()}
    doc = json.dumps(records, indent=2, sort_keys=True) + "\n"
    write_private_atomic(paths.client_file, doc.encode())


def load_client_id(paths: Paths) -> str | None:
    return _client_records(paths).get(PROVIDER, {}).get("client_id") or None


def authorize(
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> TokenBundle:
    """The browser half: consent, callback, code exchange. Stores nothing."""
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


def complete_login(
    paths: Paths,
    settings: Settings,
    alias: str,
    client_id: str,
    bundle: TokenBundle,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Account:
    """Bind a freshly exchanged ``bundle`` as ``alias``, if it really is that account.

    ``account_mismatch`` (naming both handles) when X says the token belongs
    to another handle than the alias's or the configured ``expected_handle``:
    nothing is stored, not even the client id. Otherwise the bundle becomes a
    new binding of ``alias`` under its refresh lock, with a verified row.
    """
    alias = require_x_alias(alias)
    identity = fetch_identity(bundle, transport=transport)
    check_handle(alias, identity.handle, settings)
    account = AccountRegistry(paths).bind(alias, bundle, identity, settings)
    save_client_id(paths, client_id)
    return account


def require_x_alias(alias: str) -> str:
    """``alias`` in canonical form, refused unless it names an X account."""
    canonical = canonical_alias(alias)
    if alias_provider(canonical) != PROVIDER:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"{canonical} is not an X account; X logins bind x:<handle>",
            detail={"account": canonical},
        )
    return canonical


def login(
    paths: Paths,
    settings: Settings,
    alias: str,
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
) -> Account:
    """``pulsar auth login --account x:<handle>``: consent in a browser, verify, bind."""
    require_x_alias(alias)  # refuse a bad alias before sending the human to X
    bundle = authorize(client_id, open_browser=open_browser, transport=transport)
    return complete_login(paths, settings, alias, client_id, bundle, transport=transport)
