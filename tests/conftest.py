"""Shared fixtures: a temp PULSAR_HOME, a stored token bundle, and a fake X API."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from pulsar.app.core.account import Account, AccountRegistry, FernetFileStore, TokenBundle
from pulsar.app.facade import LocalApp
from pulsar.app.runtime import LocalRuntime
from pulsar.app.settings import Settings, load_settings
from pulsar.internal.fs import Paths
from pulsar.internal.guard import scanner as guard

ACCESS = "access-token-AAAA1111"
REFRESH = "refresh-token-BBBB2222"
ROTATED_ACCESS = "access-token-CCCC3333"
ROTATED_REFRESH = "refresh-token-DDDD4444"
SECRETS = (ACCESS, REFRESH, ROTATED_ACCESS, ROTATED_REFRESH)
ALIAS = "x:constworks"


def collect(results: Any, procs: list[Any], *, timeout: float = 60.0) -> list[Any]:
    """One result per child from ``results``, failing fast when a child dies
    without answering rather than waiting out the timeout."""
    out: list[Any] = []
    deadline = time.monotonic() + timeout
    while len(out) < len(procs):
        try:
            out.append(results.get(timeout=0.2))
            continue
        except queue.Empty:
            pass
        dead = [p.exitcode for p in procs if not p.is_alive() and p.exitcode != 0]
        if dead:
            pytest.fail(f"a child exited {dead[0]} before answering")
        if time.monotonic() > deadline:
            pytest.fail(f"children answered {len(out)} of {len(procs)} in {timeout:.0f}s")
    return out


def reap(procs: list[Any]) -> None:
    """Stop and wait for every child, however the test ended."""
    for p in procs:
        if p.is_alive():
            p.terminate()
    for p in procs:
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join(5)


def register(
    paths: Paths,
    bundle: TokenBundle | None = None,
    alias: str = ALIAS,
    **row: Any,
) -> FernetFileStore:
    """Bind ``alias`` the way a login leaves it, minus the browser: its bundle
    under ``accounts/<slug>/`` and a registry row (unverified unless ``row``
    names a handle and user id). Returns the account's store."""
    store = FernetFileStore.for_account(paths, alias)
    if bundle is not None:
        store.save(bundle)
        row.setdefault("binding_id", bundle.binding_id)
        row.setdefault("scopes", tuple(bundle.scope.split()))
    AccountRegistry(paths).put(Account(alias=alias, provider=alias.partition(":")[0], **row))
    return store


def make_runtime(
    paths: Paths,
    settings: Settings | None = None,
    *,
    environ: Any = None,
    media_base: Path | None = None,
    **kwargs: Any,
) -> LocalRuntime:
    """A runtime as ``App.runtime`` builds one: the home's settings, this
    process's environment and cwd, unless a test gives its own."""
    return LocalRuntime(
        paths,
        load_settings(paths) if settings is None else settings,
        environ=os.environ if environ is None else environ,
        media_base=Path.cwd() if media_base is None else media_base,
        **kwargs,
    )


def make_app(paths: Paths, *, transport: httpx.AsyncBaseTransport | None = None) -> LocalApp:
    """The app the entry point would build over ``paths``, on ``transport`` when given."""
    return LocalApp(paths, environ=os.environ, cwd=Path.cwd(), transport=transport)


@pytest.fixture(autouse=True)
def hermetic_env(tmp_path_factory, monkeypatch) -> None:
    """No test sees the operator's home, config or Orbit state:
    HOME, XDG_CONFIG_HOME and PULSAR_HOME point at a throwaway directory, and
    the variables that would redirect the home or label writes are cleared,
    and no live credential from an earlier test is masked."""
    root = tmp_path_factory.mktemp("env")
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg"))
    monkeypatch.setenv("PULSAR_HOME", str(root / "pulsar-home"))
    for name in ("ORBIT_PLUGIN_STATE", "ORBIT_TOOL_NAME", "PULSAR_CALLER"):
        monkeypatch.delenv(name, raising=False)
    # Credentials registered for redaction are process-global: each test starts empty.
    monkeypatch.setattr(guard, "_live", set())


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
def store(paths) -> FernetFileStore:
    """The default account's credential store (``accounts/x--constworks/``)."""
    return FernetFileStore.for_account(paths, ALIAS)


@pytest.fixture
def legacy_store(paths) -> FernetFileStore:
    """The phase 1 single-account store at the home root, for migration tests."""
    return FernetFileStore(paths)


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
def authed(paths, bundle) -> TokenBundle:
    """``x:constworks`` bound, identity not yet looked up."""
    register(paths, bundle)
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
    media_finalize_info: dict[str, Any] | None = field(
        default_factory=lambda: {"state": "succeeded"}
    )
    media_status_info: list[dict[str, Any]] = field(default_factory=list)
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
            data = {"id": "710000"}
            if self.media_finalize_info is not None:
                data["processing_info"] = self.media_finalize_info
            return httpx.Response(200, json={"data": data})
        if path.endswith("/media/upload") and request.method == "GET":
            info = self.media_status_info.pop(0) if self.media_status_info else {"state": "pending"}
            return httpx.Response(200, json={"data": {"id": "710000", "processing_info": info}})
        return httpx.Response(404, json={"title": "Not Found"})


@pytest.fixture
def fake_x() -> FakeX:
    return FakeX()


class RotatingTokenEndpoint:
    """X's OAuth token endpoint with real rotation semantics.

    Each successful refresh issues a new access/refresh pair and invalidates
    the refresh token it consumed; a spent or unknown refresh token gets 400
    ``invalid_grant``, as on X. State lives in a JSON file (updated under its
    own ``flock``, standing in for X's server-side atomicity) so separate OS
    processes built from the same ``state_file`` share one endpoint.
    """

    def __init__(self, state_file: os.PathLike[str] | str, *, delay: float = 0.0) -> None:
        self.state_file = Path(state_file)
        self.delay = delay  # simulated latency, spent while the caller holds its lock

    def seed(self, refresh_token: str) -> None:
        self.state_file.write_text(
            json.dumps({"valid_refresh": refresh_token, "generation": 0, "calls": 0})
        )

    @contextlib.contextmanager
    def _state(self):
        with self.state_file.open("r+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            state = json.load(fh)
            yield state
            fh.seek(0)
            fh.truncate()
            json.dump(state, fh)

    def calls(self) -> int:
        return json.loads(self.state_file.read_text())["calls"]

    def rotate(self, refresh_token: str, *, count: bool = True) -> dict[str, Any] | None:
        """Consume ``refresh_token``: the new token response, or None if it is spent."""
        with self._state() as state:
            state["calls"] += int(count)
            if refresh_token != state["valid_refresh"]:
                return None
            state["generation"] += 1
            gen = state["generation"]
            state["valid_refresh"] = f"refresh-gen{gen}-ZZZZ"
        return {
            "token_type": "bearer",
            "expires_in": 7200,
            "access_token": f"access-gen{gen}-YYYY",
            "refresh_token": f"refresh-gen{gen}-ZZZZ",
            "scope": "tweet.read tweet.write users.read offline.access",
        }

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/oauth2/token"):
            return httpx.Response(404, json={"title": "Not Found"})
        if self.delay:
            await asyncio.sleep(self.delay)
        form = parse_qs(request.content.decode())
        issued = self.rotate(form.get("refresh_token", [""])[0])
        if issued is None:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "Value passed was invalid"},
            )
        return httpx.Response(200, json=issued)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture
def token_endpoint(tmp_path) -> RotatingTokenEndpoint:
    endpoint = RotatingTokenEndpoint(tmp_path / "x-token-endpoint.json")
    endpoint.seed(REFRESH)
    return endpoint


@pytest.fixture
def private_umask():
    """A permissive (group-writable) umask, so every 0600 must come from pulsar itself."""
    old = os.umask(0o002)
    yield
    os.umask(old)


@dataclass
class FlakyX(FakeX):
    """FakeX whose ``POST /tweets`` can fail the way real networks do.

    ``tweet_raise`` is an httpx transport error class raised on the next
    ``tweet_raise_times`` posts. With ``raise_after_accept`` the fake first
    *creates* the post (it is recorded in ``requests`` and consumes a post id)
    and only then raises — a response lost after X acted. Without it the
    request is dropped before X sees it. ``tweet_gate``, when set, holds
    every tweet POST until the event is set (for concurrency tests).
    ``tweet_raw`` replaces a successful tweet response body with raw bytes.
    """

    tweet_raise: type[httpx.TransportError] | None = None
    tweet_raise_times: int = 1
    raise_after_accept: bool = True
    tweet_gate: asyncio.Event | None = None
    tweet_raw: bytes | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle_async)

    async def handle_async(self, request: httpx.Request) -> httpx.Response:
        if not (request.url.path.endswith("/tweets") and request.method == "POST"):
            return self.handle(request)
        if self.tweet_gate is not None:
            await self.tweet_gate.wait()
        if self.tweet_raise is not None and self.tweet_raise_times > 0:
            self.tweet_raise_times -= 1
            if self.raise_after_accept:
                self.handle(request)
            raise self.tweet_raise("simulated transport failure", request=request)
        resp = self.handle(request)
        if self.tweet_raw is not None and resp.status_code < 300:
            return httpx.Response(resp.status_code, content=self.tweet_raw)
        return resp

    def posts(self) -> list[httpx.Request]:
        """Tweet POSTs that reached X."""
        return self.calls("POST", "/tweets")


@pytest.fixture
def flaky_x() -> FlakyX:
    return FlakyX()
