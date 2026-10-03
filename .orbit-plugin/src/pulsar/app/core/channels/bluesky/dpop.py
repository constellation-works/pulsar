"""``Es256Proof``: the ``DpopProof`` atproto OAuth needs, over a P-256 key.

A DPoP proof (RFC 9449) is a JWT the client signs per request: its header
carries the public key, its claims the method (``htm``), the URL without query
(``htu``), the time (``iat``), a unique ``jti``, the server's ``nonce`` when it
sent one, and ``ath``, the hash of the access token the request carries. A
token bound to the key is useless without it.

The login stores the key with the account's credentials and builds the proof
from it; the key stays inside this object (only ``public_jwk`` comes out).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _segment(value: dict[str, object]) -> str:
    return _b64(json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"))


class Es256Proof:
    """Signs DPoP proofs with ``key`` (ES256)."""

    def __init__(
        self,
        key: ec.EllipticCurvePrivateKey,
        *,
        now: Callable[[], float] = time.time,
        jti: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        if not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("a DPoP key for atproto must be P-256 (ES256)")
        self._key = key
        self._now = now
        self._jti = jti
        numbers = key.public_key().public_numbers()
        self.public_jwk: dict[str, str] = {
            "kty": "EC",
            "crv": "P-256",
            "x": _b64(numbers.x.to_bytes(32, "big")),
            "y": _b64(numbers.y.to_bytes(32, "big")),
        }

    def __call__(
        self, method: str, url: str, *, nonce: str | None, access_token: str | None
    ) -> str:
        header: dict[str, object] = {"typ": "dpop+jwt", "alg": "ES256", "jwk": self.public_jwk}
        claims: dict[str, object] = {
            "jti": self._jti(),
            "htm": method.upper(),
            "htu": url,
            "iat": int(self._now()),
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if access_token is not None:
            claims["ath"] = _b64(hashlib.sha256(access_token.encode("ascii")).digest())
        signing_input = f"{_segment(header)}.{_segment(claims)}"
        der = self._key.sign(signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        return f"{signing_input}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
