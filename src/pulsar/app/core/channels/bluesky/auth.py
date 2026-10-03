"""atproto OAuth (PAR + PKCE + DPoP), run by a human on the host. Stores nothing.

``pulsar auth login --account bsky:<handle>`` resolves the handle to its DID,
the DID to its document and PDS, and the PDS to its authorization server
(``/.well-known/oauth-protected-resource``, then
``/.well-known/oauth-authorization-server``). It mints a DPoP key (P-256)
for this login, pushes the authorization request (PAR) with PKCE (S256)
under a DPoP proof, sends the human to approve it in a browser, catches the
redirect on the shared loopback listener (``channels.loopback``; its
``state`` and ``iss`` must be this login's) and exchanges the code for a
DPoP-bound token pair. Every request to the authorization server carries a
fresh proof; when the server answers ``use_dpop_nonce`` the request is
repeated once with the nonce it sent (it refused the first, so nothing
happens twice).

Who the token belongs to comes from the token, never from the handle typed:
the token response's ``sub`` DID is resolved to its document, whose handle
counts only when it resolves back to the same DID (otherwise the identity has
no handle), and the server that issued the token must be the one that DID's
PDS names. The app then refuses a handle other than the alias's before
anything is stored (``account_mismatch``), as for X.

The client id is the URL of a client-metadata document. By default a login is
the spec's loopback development client (``http://localhost``, with the
redirect URI and scope in its query), which needs no hosted document and
suits a CLI on one host. Production use needs a hosted client-metadata
document, an https URL a human publishes; ``check_client_id`` accepts either.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from pulsar.internal.errors import API_ERROR, INVALID_ARGUMENT, PulsarError
from pulsar.internal.fs import as_list, as_object
from pulsar.internal.guard import redact

from ..contract import Authorized, Identity
from ..credentials import TokenBundle
from ..loopback import WAIT_SECONDS, CallbackServer, notify_stderr, open_quietly, redirect_uri
from .client import PROVIDER_TEXT_LIMIT, error_detail, htu, origin, wants_nonce
from .config import BSKY_SERVICE, LOOPBACK_CLIENT, PLC_DIRECTORY, PROVIDER, SCOPE
from .dpop import Es256Proof, new_key

TIMEOUT_SECONDS = 30.0
_PLC = re.compile(r"did:plc:[a-z2-7]+")
_WEB = re.compile(r"did:web:[a-z0-9.-]+")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _failed(message: str, **detail: Any) -> PulsarError:
    return PulsarError(API_ERROR, f"{message}; nothing was stored", detail=detail or None)


def loopback_client_id(redirect: str | None = None) -> str:
    """The loopback development client id, naming ``redirect`` and the scope."""
    query = urlencode({"redirect_uri": redirect or redirect_uri(), "scope": SCOPE})
    return f"{LOOPBACK_CLIENT}?{query}"


def check_client_id(client_id: str) -> str:
    """``client_id`` if it is a client id atproto OAuth accepts, else ``invalid_argument``:
    the loopback client (``http://localhost``, no port or path) or the https URL of
    a client-metadata document (with a path, without a fragment)."""
    parts = urlsplit(client_id)
    loopback = (
        parts.scheme == "http"
        and parts.netloc == "localhost"
        and parts.path in ("", "/")
        and not parts.fragment
    )
    hosted = (
        parts.scheme == "https"
        and bool(parts.hostname)
        and parts.path not in ("", "/")
        and not parts.fragment
    )
    if not (loopback or hosted):
        raise PulsarError(
            INVALID_ARGUMENT,
            "a Bluesky client id is the https URL of a client-metadata document, or the "
            f"loopback development client {LOOPBACK_CLIENT}",
            detail={"client_id": client_id},
        )
    return client_id


@dataclass(frozen=True)
class AuthServer:
    issuer: str
    par_endpoint: str
    authorization_endpoint: str
    token_endpoint: str


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


def _https(value: object) -> str | None:
    if isinstance(value, str) and urlsplit(value).scheme == "https" and urlsplit(value).hostname:
        return value
    return None


def _pds(did: str, doc: Mapping[str, Any]) -> str:
    """The PDS a DID document names (its ``#atproto_pds`` service)."""
    for raw in as_list(doc.get("service")):
        entry = as_object(raw)
        if (
            entry is not None
            and str(entry.get("id", "")).endswith("#atproto_pds")
            and entry.get("type") == "AtprotoPersonalDataServer"
            and (endpoint := _https(entry.get("serviceEndpoint"))) is not None
        ):
            return endpoint.rstrip("/")
    raise _failed(f"the DID document of {did} names no PDS", did=did)


def _claimed_handle(doc: Mapping[str, Any]) -> str | None:
    """The handle a DID document claims (``alsoKnownAs: at://<handle>``), unverified."""
    for aka in as_list(doc.get("alsoKnownAs")):
        if isinstance(aka, str) and aka.startswith("at://") and len(aka) > len("at://"):
            return aka.removeprefix("at://").lower()
    return None


class _Session:
    """One login's requests: identity lookups (remembered for the login) and the
    authorization server's DPoP-signed POSTs, with its latest nonce."""

    def __init__(self, http: httpx.Client, proof: Es256Proof, *, resolver: str) -> None:
        self._http = http
        self._proof = proof
        self._resolver = resolver.rstrip("/")
        self._nonces: dict[str, str] = {}
        self._dids: dict[str, str | None] = {}
        self._docs: dict[str, dict[str, Any]] = {}
        self._servers: dict[str, AuthServer] = {}

    def _get(self, url: str, what: str, **params: str) -> httpx.Response:
        try:
            return self._http.get(url, params=params or None)
        except httpx.HTTPError as exc:
            raise _failed(f"could not fetch {what} ({exc.__class__.__name__})", url=url) from exc

    def _json(self, url: str, what: str) -> dict[str, Any]:
        resp = self._get(url, what)
        if resp.status_code != 200:
            raise _failed(f"could not fetch {what} (HTTP {resp.status_code})", url=url)
        try:
            body = as_object(resp.json())
        except ValueError:
            body = None
        if body is None:
            raise _failed(f"{what} is not a JSON object", url=url)
        return body

    # -- identity -------------------------------------------------------------

    def handle_did(self, handle: str) -> str | None:
        """The DID ``handle`` resolves to, or None: its ``/.well-known/atproto-did``,
        else the resolver's ``com.atproto.identity.resolveHandle``."""
        if handle not in self._dids:
            self._dids[handle] = self._well_known_did(handle) or self._resolved_did(handle)
        return self._dids[handle]

    def _well_known_did(self, handle: str) -> str | None:
        try:
            resp = self._http.get(f"https://{handle}/.well-known/atproto-did")
        except httpx.HTTPError:
            return None
        did = resp.text.strip() if resp.status_code == 200 else ""
        return did if did.startswith("did:") else None

    def _resolved_did(self, handle: str) -> str | None:
        url = f"{self._resolver}/xrpc/com.atproto.identity.resolveHandle"
        try:
            resp = self._http.get(url, params={"handle": handle})
            body = as_object(resp.json()) if resp.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None
        did = body.get("did") if body is not None else None
        return did if isinstance(did, str) and did.startswith("did:") else None

    def did_document(self, did: str) -> dict[str, Any]:
        doc = self._docs.get(did)
        if doc is not None:
            return doc
        if _PLC.fullmatch(did):
            url = f"{PLC_DIRECTORY}/{did}"
        elif _WEB.fullmatch(did):
            url = f"https://{did.removeprefix('did:web:')}/.well-known/did.json"
        else:
            raise _failed(f"pulsar cannot resolve {did!r} (only did:plc and did:web)", did=did)
        doc = self._json(url, f"the DID document of {did}")
        if doc.get("id") != did:
            raise _failed(f"the DID document fetched for {did} is another DID's", did=did)
        self._docs[did] = doc
        return doc

    def auth_server(self, pds: str) -> AuthServer:
        """The authorization server ``pds`` names, from both metadata documents."""
        server = self._servers.get(pds)
        if server is not None:
            return server
        resource = self._json(
            f"{pds}/.well-known/oauth-protected-resource", f"the OAuth metadata of {pds}"
        )
        servers = as_list(resource.get("authorization_servers"))
        issuer = _https(servers[0]) if servers else None
        if issuer is None:
            raise _failed(f"{pds} names no authorization server", pds=pds)
        meta = self._json(
            f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
            f"the metadata of {issuer}",
        )
        endpoints = {
            name: _https(meta.get(name))
            for name in (
                "pushed_authorization_request_endpoint",
                "authorization_endpoint",
                "token_endpoint",
            )
        }
        missing = sorted(name for name, url in endpoints.items() if url is None)
        algs = as_list(meta.get("dpop_signing_alg_values_supported"))
        if meta.get("issuer") != issuer or missing or "ES256" not in algs:
            raise _failed(
                f"{issuer} is not an atproto authorization server pulsar can use (issuer, "
                "PAR, authorization and token endpoints, and ES256 DPoP are required)",
                issuer=issuer,
                missing=missing,
            )
        server = AuthServer(
            issuer=issuer,
            par_endpoint=str(endpoints["pushed_authorization_request_endpoint"]),
            authorization_endpoint=str(endpoints["authorization_endpoint"]),
            token_endpoint=str(endpoints["token_endpoint"]),
        )
        self._servers[pds] = server
        return server

    # -- the authorization server ---------------------------------------------

    def post(self, url: str, form: Mapping[str, str], what: str) -> httpx.Response:
        """POST ``form`` with a DPoP proof; once more when the server wants a fresh nonce."""
        nonced = False
        while True:
            nonce = self._nonces.get(origin(url))
            proof = self._proof("POST", htu(url), nonce=nonce, access_token=None)
            try:
                resp = self._http.post(url, data=dict(form), headers={"DPoP": proof})
            except httpx.HTTPError as exc:
                raise _failed(f"{what} failed ({exc.__class__.__name__})", url=url) from exc
            if nonce := resp.headers.get("DPoP-Nonce"):
                self._nonces[origin(url)] = nonce
            if nonced or not wants_nonce(resp):
                return resp
            nonced = True


def _answer(resp: httpx.Response, what: str) -> dict[str, Any]:
    """A successful authorization-server answer as a JSON object, else ``api_error``."""
    if resp.status_code not in (200, 201):
        raise _failed(f"{what} was refused (HTTP {resp.status_code})", **error_detail(resp))
    try:
        body = as_object(resp.json())
    except ValueError:
        body = None
    if body is None:
        raise _failed(f"{what} answered with no JSON object")
    return body


def _bounded(value: str) -> str:
    return redact(value)[:PROVIDER_TEXT_LIMIT]


class Redirect(Protocol):
    """Where the browser comes back to: ``loopback.CallbackServer``, or a test's."""

    def wait(self, timeout: float) -> dict[str, list[str]]: ...

    def server_close(self) -> None: ...


class BlueskyLogin:
    """``AuthFlow`` for Bluesky accounts: atproto OAuth on the loopback redirect.

    ``transport`` replaces the network in tests, and ``callback`` the loopback
    listener (it is handed the login's ``state``); ``resolver`` is the service
    asked to resolve a handle that has no ``/.well-known/atproto-did``.
    """

    provider = PROVIDER

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: str = BSKY_SERVICE,
        callback: Callable[[str], Redirect] | None = None,
        wait_seconds: float = WAIT_SECONDS,
    ) -> None:
        self._transport = transport
        self._resolver = resolver
        self._callback: Callable[[str], Redirect] = callback or CallbackServer
        self._wait_seconds = wait_seconds

    def default_client_id(self) -> str:
        return loopback_client_id()

    def authorize(
        self,
        handle: str,
        client_id: str,
        *,
        open_browser: bool = True,
        notify: Callable[[str], None] = notify_stderr,
    ) -> Authorized:
        """Consent in a browser as ``handle``, then the code exchange and the
        identity lookup of the token's ``sub``. Stores nothing."""
        check_client_id(client_id)
        handle = handle.lower()
        key = new_key()
        with httpx.Client(transport=self._transport, timeout=TIMEOUT_SECONDS) as http:
            session = _Session(http, Es256Proof.from_stored(key), resolver=self._resolver)
            did = session.handle_did(handle)
            if did is None:
                raise _failed(f"@{handle} does not resolve to an atproto account", handle=handle)
            server = session.auth_server(_pds(did, session.did_document(did)))
            pkce = Pkce.generate()
            redirect = redirect_uri()
            par = _answer(
                session.post(
                    server.par_endpoint,
                    {
                        "response_type": "code",
                        "client_id": client_id,
                        "redirect_uri": redirect,
                        "scope": SCOPE,
                        "state": pkce.state,
                        "code_challenge": pkce.challenge,
                        "code_challenge_method": "S256",
                        "login_hint": handle,
                    },
                    "the pushed authorization request",
                ),
                "the pushed authorization request",
            )
            request_uri = par.get("request_uri")
            if not isinstance(request_uri, str) or not request_uri:
                raise _failed("the pushed authorization request returned no request_uri")
            query = urlencode({"client_id": client_id, "request_uri": request_uri})
            code = self._consent(
                f"{server.authorization_endpoint}?{query}",
                pkce.state,
                server.issuer,
                open_browser=open_browser,
                notify=notify,
            )
            tokens = _answer(
                session.post(
                    server.token_endpoint,
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect,
                        "code_verifier": pkce.verifier,
                        "client_id": client_id,
                    },
                    "the token exchange",
                ),
                "the token exchange",
            )
            identity, pds = self._identity(session, _subject(tokens), server.issuer)
        try:
            bundle = TokenBundle.from_token_response(tokens, client_id=client_id)
        except (KeyError, ValueError, TypeError) as exc:
            raise _failed("the token exchange answered without a usable access token") from exc
        bundle.token_type = "DPoP"
        bundle.dpop_key, bundle.service = key, pds
        bundle.token_url = server.token_endpoint
        return Authorized(
            bundle=bundle, identity=identity, client_id=client_id, redirect_uri=redirect
        )

    def _consent(
        self,
        url: str,
        state: str,
        issuer: str,
        *,
        open_browser: bool,
        notify: Callable[[str], None],
    ) -> str:
        """The human approves in a browser; the authorization code of the redirect."""
        server = self._callback(state)  # listening before the human can be redirected
        try:
            notify(f"Open this URL and approve as the account that should post:\n\n  {url}\n")
            if open_browser:
                threading.Thread(target=open_quietly, args=(url,), daemon=True).start()
            query = server.wait(self._wait_seconds)
        finally:
            server.server_close()
        if "error" in query:
            reason = _bounded(query.get("error_description", query["error"])[0])
            raise _failed(f"Bluesky denied authorization: {reason}")
        if query.get("iss", [""])[0] != issuer:
            raise _failed(
                f"the redirect does not come from {issuer}, the server this login asked",
                iss=_bounded(query.get("iss", [""])[0]) or None,
            )
        if "code" not in query:
            raise _failed("the browser redirect carried no authorization code")
        return query["code"][0]

    @staticmethod
    def _identity(session: _Session, sub: str, issuer: str) -> tuple[Identity, str]:
        """Who ``sub`` is, and its PDS. The handle counts only if it resolves back
        to ``sub``; ``issuer`` must be the authorization server of ``sub``'s PDS."""
        doc = session.did_document(sub)
        pds = _pds(sub, doc)
        if session.auth_server(pds).issuer != issuer:
            raise _failed(
                f"{issuer} issued a token for {sub}, whose PDS names another authorization server",
                sub=sub,
                issuer=issuer,
            )
        claimed = _claimed_handle(doc)
        verified = claimed is not None and session.handle_did(claimed) == sub
        return Identity(provider_user_id=sub, handle=claimed if verified and claimed else ""), pds


def _subject(tokens: Mapping[str, Any]) -> str:
    """The token response's ``sub``, refused unless the pair is DPoP-bound atproto."""
    sub = tokens.get("sub")
    if str(tokens.get("token_type", "")).lower() != "dpop":
        raise _failed("the token exchange returned a token that is not DPoP-bound")
    if "atproto" not in str(tokens.get("scope", "")).split():
        raise _failed("the token exchange returned a token without the atproto scope")
    if not isinstance(sub, str) or not sub.startswith("did:"):
        raise _failed("the token exchange named no account (sub)")
    return sub
