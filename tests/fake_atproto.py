"""A scripted atproto network for Bluesky logins, for tests: no network.

It resolves handles (``https://<handle>/.well-known/atproto-did``) and DIDs
(``plc.directory``), serves each PDS's protected-resource metadata and its
authorization server: metadata, PAR, a browser that approves a pushed
request, and the token endpoint (authorization code and refresh). It checks
what a real one would refuse: a DPoP proof that is unsigned, for another
method or URL, without the server's current nonce (``use_dpop_nonce``), or
from another key than the login's; a PKCE verifier that does not match; a
code or refresh token used twice. XRPC calls to the PDS go to a ``FakeBsky``
that requires DPoP proofs with its own nonce.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from .conftest import ACCESS, REFRESH, ROTATED_ACCESS, ROTATED_REFRESH
from .fake_bsky import ALICE, ALICE_DID, DID, HANDLE, FakeBsky

PDS = "https://pds.example.test"
ISSUER = "https://auth.example.test"
# A second PDS whose accounts another authorization server issues for.
OTHER_PDS = "https://pds.other.test"
OTHER_ISSUER = "https://auth.other.test"
EVE = "eve.other.test"
EVE_DID = "did:plc:eve3kq7vymdyb2oh6ngxa4tz"
AS_NONCE = "as-nonce-2kq4"
PDS_NONCE = "pds-nonce-7x2k"
SCOPE = "atproto transition:generic"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def thumbprint(jwk: dict[str, str]) -> str:
    """RFC 7638: what binds a token to its DPoP key (``cnf.jkt``)."""
    canonical = json.dumps({k: jwk[k] for k in ("crv", "kty", "x", "y")}, separators=(",", ":"))
    return _b64(hashlib.sha256(canonical.encode()).digest())


def verify_proof(proof: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The proof's (header, claims), after checking its ES256 signature by its own key."""
    head, body, sig = proof.split(".")
    header, claims = json.loads(_unb64(head)), json.loads(_unb64(body))
    assert header["typ"] == "dpop+jwt" and header["alg"] == "ES256"
    jwk = header["jwk"]
    x, y = (int.from_bytes(_unb64(jwk[c]), "big") for c in ("x", "y"))
    key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    raw = _unb64(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    return header, claims


def _error(status: int, name: str, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"error": name, "error_description": name}, headers=headers)


def _document(did: str, handle: str, pds: str) -> dict[str, Any]:
    return {
        "@context": ["https://www.w3.org/ns/did/v1"],
        "id": did,
        "alsoKnownAs": [f"at://{handle}"],
        "service": [
            {"id": "#atproto_pds", "type": "AtprotoPersonalDataServer", "serviceEndpoint": pds}
        ],
    }


@dataclass
class FakeAtproto:
    pds: FakeBsky = field(default_factory=lambda: FakeBsky(dpop_nonce=PDS_NONCE))
    # handle -> DID, served at https://<handle>/.well-known/atproto-did
    handles: dict[str, str] = field(
        default_factory=lambda: {HANDLE: DID, ALICE: ALICE_DID, EVE: EVE_DID}
    )
    documents: dict[str, dict[str, Any]] = field(
        default_factory=lambda: {
            DID: _document(DID, HANDLE, PDS),
            ALICE_DID: _document(ALICE_DID, ALICE, PDS),
            EVE_DID: _document(EVE_DID, EVE, OTHER_PDS),
        }
    )
    # whom the token exchange says the token is for
    sub: str = DID
    nonce: str = AS_NONCE
    # the nonce the server moves to while the human is at the browser, if it does
    next_nonce: str | None = None
    # the browser: an error to answer with, or another issuer to claim
    deny: str | None = None
    redirect_iss: str | None = None
    pars: dict[str, dict[str, str]] = field(default_factory=dict)  # by request_uri
    codes: dict[str, dict[str, str]] = field(default_factory=dict)
    refresh_token: str | None = None  # the one live refresh token
    bound_jkt: str | None = None  # the key the live tokens are bound to
    nonce_challenges: list[str] = field(default_factory=list)  # paths answered use_dpop_nonce
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_request)

    def posts(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == "POST" and r.url.path == path]

    @staticmethod
    def form(request: httpx.Request) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

    # -- the browser ------------------------------------------------------------------

    def approve(self, url: str) -> dict[str, list[str]]:
        """The human opens ``url`` and approves: the redirect's query."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        par = self.pars[query["request_uri"]]
        assert query["client_id"] == par["client_id"]
        issuer = self.redirect_iss or ISSUER
        self.nonce = self.next_nonce or self.nonce
        if self.deny is not None:
            return {"error": [self.deny], "state": [par["state"]], "iss": [issuer]}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = par
        return {"code": [code], "state": [par["state"]], "iss": [issuer]}

    # -- the network ------------------------------------------------------------------

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if path == "/.well-known/atproto-did":
            did = self.handles.get(host)
            return httpx.Response(200, text=did) if did else httpx.Response(404)
        if host == "plc.directory":
            doc = self.documents.get(path.removeprefix("/"))
            return httpx.Response(200, json=doc) if doc else httpx.Response(404)
        issuers = {urlsplit(PDS).hostname: ISSUER, urlsplit(OTHER_PDS).hostname: OTHER_ISSUER}
        if host in issuers:
            if path == "/.well-known/oauth-protected-resource":
                resource = f"https://{host}"
                return httpx.Response(
                    200, json={"resource": resource, "authorization_servers": [issuers[host]]}
                )
            return self.pds.handle_request(request)
        issuer = f"https://{host}"
        if issuer in issuers.values():
            if path == "/.well-known/oauth-authorization-server":
                return httpx.Response(200, json=self.metadata(issuer))
            if issuer == ISSUER and path == "/oauth/par":
                return self._par(request)
            if issuer == ISSUER and path == "/oauth/token":
                return self._token(request)
        return httpx.Response(404)

    @staticmethod
    def metadata(issuer: str) -> dict[str, Any]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "pushed_authorization_request_endpoint": f"{issuer}/oauth/par",
            "require_pushed_authorization_requests": True,
            "dpop_signing_alg_values_supported": ["ES256"],
            "authorization_response_iss_parameter_supported": True,
            "scopes_supported": ["atproto", "transition:generic"],
        }

    def _dpop(self, request: httpx.Request) -> str | httpx.Response:
        """The proof's key thumbprint, or the refusal a real server answers."""
        proof = request.headers.get("DPoP")
        if proof is None:
            return _error(400, "invalid_dpop_proof")
        try:
            header, claims = verify_proof(proof)
        except (InvalidSignature, ValueError, KeyError, AssertionError):
            return _error(400, "invalid_dpop_proof")
        url = request.url
        htu = f"{url.scheme}://{url.host}{url.path}"
        if claims["htm"] != request.method or claims["htu"] != htu:
            return _error(400, "invalid_dpop_proof")
        if "ath" in claims or not claims.get("jti"):
            return _error(400, "invalid_dpop_proof")
        if claims.get("nonce") != self.nonce:
            self.nonce_challenges.append(request.url.path)
            return _error(400, "use_dpop_nonce", **{"DPoP-Nonce": self.nonce})
        return thumbprint(header["jwk"])

    def _par(self, request: httpx.Request) -> httpx.Response:
        jkt = self._dpop(request)
        if isinstance(jkt, httpx.Response):
            return jkt
        form = self.form(request)
        required = {"response_type", "client_id", "redirect_uri", "scope", "state"}
        required |= {"code_challenge", "code_challenge_method"}
        if not required <= set(form) or form["code_challenge_method"] != "S256":
            return _error(400, "invalid_request")
        client = urlsplit(form["client_id"])
        if client.netloc == "localhost":  # the loopback client names its redirect
            allowed = parse_qs(client.query).get("redirect_uri", [])
            if form["redirect_uri"] not in allowed:
                return _error(400, "invalid_redirect_uri")
        if form["scope"] != SCOPE:
            return _error(400, "invalid_scope")
        request_uri = f"urn:ietf:params:oauth:request_uri:req-{len(self.pars) + 1}"
        self.pars[request_uri] = {**form, "jkt": jkt}
        return httpx.Response(201, json={"request_uri": request_uri, "expires_in": 299})

    def _token(self, request: httpx.Request) -> httpx.Response:
        jkt = self._dpop(request)
        if isinstance(jkt, httpx.Response):
            return jkt
        form = self.form(request)
        if form.get("grant_type") == "authorization_code":
            par = self.codes.pop(form.get("code", ""), None)
            if par is None or form.get("redirect_uri") != par["redirect_uri"]:
                return _error(400, "invalid_grant")
            verifier = form.get("code_verifier", "")
            if _b64(hashlib.sha256(verifier.encode()).digest()) != par["code_challenge"]:
                return _error(400, "invalid_grant")
            if form.get("client_id") != par["client_id"] or jkt != par["jkt"]:
                return _error(400, "invalid_grant")
            access, refresh = ACCESS, REFRESH
        elif form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") != self.refresh_token or jkt != self.bound_jkt:
                return _error(400, "invalid_grant")
            access, refresh = ROTATED_ACCESS, ROTATED_REFRESH
        else:
            return _error(400, "unsupported_grant_type")
        self.refresh_token, self.bound_jkt = refresh, jkt
        return httpx.Response(
            200,
            json={
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "DPoP",
                "expires_in": 3600,
                "scope": SCOPE,
                "sub": self.sub,
            },
        )


class Browser:
    """The human: reads the consent URL ``notify`` shows, approves it on ``fake``,
    and the redirect comes back to the listener the login opened."""

    def __init__(self, fake: FakeAtproto) -> None:
        self.fake = fake
        self.shown: list[str] = []
        self.states: list[str] = []

    def notify(self, message: str) -> None:
        self.shown.append(message)

    @property
    def url(self) -> str:
        return self.shown[-1].split()[-1]

    def redirect(self, state: str) -> _Redirect:
        self.states.append(state)
        return _Redirect(self)


class _Redirect:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.closed = False

    def wait(self, timeout: float) -> dict[str, list[str]]:
        return self.browser.fake.approve(self.browser.url)

    def server_close(self) -> None:
        self.closed = True
