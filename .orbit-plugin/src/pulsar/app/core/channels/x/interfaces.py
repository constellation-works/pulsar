"""``XApi``: the X v2 API as pulsar uses it. ``XClient`` is the implementation
(httpx, with token refresh); the meaning of each method is documented there."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from ..credentials import CredentialStore, TokenBundle


class XApi(Protocol):
    """One account's authenticated X API."""

    @property
    def store(self) -> CredentialStore: ...

    async def request(
        self, method: str, path: str, *, non_idempotent: bool = False, **kwargs: Any
    ) -> httpx.Response: ...

    async def refresh(self, bundle: TokenBundle) -> TokenBundle: ...

    async def me(self) -> dict[str, str]: ...

    async def create_post(
        self,
        text: str,
        *,
        reply_to_post_id: str | None = None,
        quote_post_id: str | None = None,
        media_ids: list[str] | None = None,
    ) -> dict[str, str]: ...

    async def delete_post(self, post_id: str) -> bool: ...

    async def upload_media(
        self, data: bytes, mime: str, *, chunk_size: int | None = None
    ) -> tuple[str, str]: ...

    async def aclose(self) -> None: ...
