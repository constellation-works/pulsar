"""Shared fixtures: a temp PULSAR_HOME, a stored token bundle, and a fake X API."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from pulsar.config import Paths
from pulsar.store import TokenBundle, TokenStore

ACCESS = "access-token-AAAA1111"
REFRESH = "refresh-token-BBBB2222"
ROTATED_ACCESS = "access-token-CCCC3333"
ROTATED_REFRESH = "refresh-token-DDDD4444"
SECRETS = (ACCESS, REFRESH, ROTATED_ACCESS, ROTATED_REFRESH)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def paths(tmp_path, monkeypatch) -> Paths:
    home = tmp_path / "pulsar-home"
    monkeypatch.setenv("PULSAR_HOME", str(home))
    monkeypatch.delenv("PULSAR_CALLER", raising=False)
    return Paths(home)


@pytest.fixture
def store(paths) -> TokenStore:
    return TokenStore(paths)


@pytest.fixture
def bundle() -> TokenBundle:
    return TokenBundle(
        access_token=ACCESS,
        refresh_token=REFRESH,
        expires_at=time.time() + 3600,
        scope="tweet.read tweet.write users.read offline.access",
        client_id="client-xyz",
    )


@pytest.fixture
def authed(store, bundle) -> TokenBundle:
    store.save(bundle)
    return bundle


@dataclass
class FakeX:
    """Scripted X API. Records every request; behaviour is tweakable per test."""

    username: str = "constworks"
    user_id: str = "1234567890"
    next_post_id: int = 100
    refresh_status: int = 200
    fail_auth_once: bool = False
    tweet_status: int | None = None
    tweet_body: dict[str, Any] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def calls(
        self, method: str | None = None, path_suffix: str | None = None
    ) -> list[httpx.Request]:
        out = []
        for r in self.requests:
            if method and r.method != method:
                continue
            if path_suffix and not r.url.path.endswith(path_suffix):
                continue
            out.append(r)
        return out

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/oauth2/token"):
            if self.refresh_status != 200:
                return httpx.Response(self.refresh_status, json={"error": "invalid_request"})
            return httpx.Response(
                200,
                json={
                    "token_type": "bearer",
                    "expires_in": 7200,
                    "access_token": ROTATED_ACCESS,
                    "refresh_token": ROTATED_REFRESH,
                    "scope": "tweet.read tweet.write users.read offline.access",
                },
            )
        auth = request.headers.get("Authorization", "")
        if self.fail_auth_once and auth == f"Bearer {ACCESS}":
            self.fail_auth_once = False
            return httpx.Response(401, json={"title": "Unauthorized"})
        if auth not in (f"Bearer {ACCESS}", f"Bearer {ROTATED_ACCESS}"):
            return httpx.Response(401, json={"title": "Unauthorized"})
        if path.endswith("/users/me"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": self.user_id,
                        "username": self.username,
                        "name": "Constellation Works",
                    }
                },
            )
        if path.endswith("/tweets") and request.method == "POST":
            if self.tweet_status:
                return httpx.Response(self.tweet_status, json=self.tweet_body or {"detail": "nope"})
            body = json.loads(request.content)
            self.next_post_id += 1
            return httpx.Response(
                201, json={"data": {"id": str(self.next_post_id), "text": body["text"]}}
            )
        if "/tweets/" in path and request.method == "DELETE":
            return httpx.Response(200, json={"data": {"deleted": True}})
        if path.endswith("/media/upload/initialize"):
            return httpx.Response(200, json={"data": {"id": "710000", "media_key": "3_710000"}})
        if path.endswith("/append"):
            return httpx.Response(204)
        if path.endswith("/finalize"):
            return httpx.Response(
                200, json={"data": {"id": "710000", "processing_info": {"state": "succeeded"}}}
            )
        return httpx.Response(404, json={"title": "Not Found"})


@pytest.fixture
def fake_x() -> FakeX:
    return FakeX()
