"""``BlueskyApi``: an account's PDS as pulsar uses it (XRPC queries, procedures and
blob uploads), and ``DpopProof``, the hook that signs a request for a DPoP-bound
token. ``BlueskyClient`` is the implementation; the meaning of each method is
documented there."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from ..credentials import CredentialStore, TokenBundle


class DpopProof(Protocol):
    """Signs one request's DPoP proof (RFC 9449) with the key the token is bound to.

    ``url`` is the request URL without query or fragment (the proof's ``htu``);
    ``nonce`` the server's latest ``DPoP-Nonce``, if it sent one;
    ``access_token`` the token the request carries (hashed into ``ath``), None
    on a token request. Returns the proof JWT for the ``DPoP`` header. The key
    never leaves the object that holds it.
    """

    def __call__(
        self, method: str, url: str, *, nonce: str | None, access_token: str | None
    ) -> str: ...


class BlueskyApi(Protocol):
    """One account's authenticated PDS."""

    @property
    def store(self) -> CredentialStore: ...

    async def query(self, nsid: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]: ...

    async def procedure(
        self, nsid: str, body: Mapping[str, Any], *, non_idempotent: bool = False
    ) -> dict[str, Any]: ...

    async def upload_blob(self, data: bytes, mime: str) -> dict[str, Any]: ...

    async def me(self) -> dict[str, str]: ...

    async def refresh(self, bundle: TokenBundle) -> TokenBundle: ...

    async def aclose(self) -> None: ...
