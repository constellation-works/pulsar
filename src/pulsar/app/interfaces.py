"""``App`` and ``Runtime``: what the front ends and the verbs code against.

``LocalApp`` and ``LocalRuntime`` implement them over one home on this host;
the meaning of each member is documented there. Only the entry point
(``pulsar.main``) and ``LocalApp.runtime`` name the implementations.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any, Protocol, Self

from pulsar.core import Account, AccountRegistry, Bound, Ledger, Paths, Plan, Publisher, Settings
from pulsar.providers.x import XApi

# A verb's result: the report, and the exit code (0 settled and healthy, 1 not).
Report = tuple[dict[str, Any], int]


class Runtime(Protocol):
    """Everything the verbs and tools need, over one home."""

    @property
    def paths(self) -> Paths: ...

    @property
    def settings(self) -> Settings: ...

    @property
    def registry(self) -> AccountRegistry: ...

    @property
    def ledger(self) -> Ledger: ...

    @property
    def publisher(self) -> Publisher: ...

    def caller(self, explicit: str | None, default: str = "unknown") -> str: ...

    def account(self, alias: str | None = None) -> Account: ...

    def client_for(self, alias: str) -> XApi: ...

    def watch_expiry(self, alias: str) -> AbstractAsyncContextManager[None]: ...

    async def identity(self, alias: str | None = None, *, live: bool = False) -> Account: ...

    async def whoami(self, alias: str | None = None, *, live: bool = False) -> dict[str, str]: ...

    async def writer(self, alias: str | None) -> tuple[Account, XApi, dict[str, str]]: ...

    def offline_bound(self, alias: str) -> Bound: ...

    async def bound(self, alias: str | None) -> Bound: ...

    def plan_targets(self, plan: Plan, account: str | None) -> tuple[Plan, list[str]]: ...

    async def aclose(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *_exc: object) -> None: ...


class App(Protocol):
    """pulsar's verbs, bound to one home: what a front end is handed."""

    @property
    def paths(self) -> Paths: ...

    @property
    def environ(self) -> Mapping[str, str]: ...

    @property
    def home(self) -> Path: ...

    def runtime(
        self,
        *,
        read_only: bool = False,
        settings: Settings | None = None,
        media_base: Path | None = None,
    ) -> Runtime: ...

    def default_account(self) -> str | None: ...

    def remembered_client_id(self) -> str | None: ...

    def migrate_legacy_quietly(self) -> str | None: ...

    def login(self, alias: str, client_id: str, *, open_browser: bool) -> Report: ...

    async def auth_status(self, *, account: str | None = None, live: bool = False) -> Report: ...

    def attention(self, entry: dict[str, Any]) -> str | None: ...

    def logout(self, account: str | None = None) -> Report: ...

    def auth_migrate(self, *, account: str | None = None, confirm: bool = False) -> Report: ...

    def status(self, *, account: str | None = None) -> Report: ...

    def history(self, *, account: str | None = None, limit: int = ...) -> Report: ...

    async def validate(self, plan: Path, *, account: str | None = None) -> Report: ...

    async def publish(
        self,
        plan: Path,
        *,
        account: str | None = None,
        idempotency_key: str | None = None,
        caller: str | None = None,
        confirm: bool = False,
    ) -> Report: ...

    async def reconcile(self, *, account: str | None = None) -> Report: ...

    async def import_posted(
        self, source: Path, *, account: str | None = None, confirm: bool = False
    ) -> Report: ...

    def migrate(self, *, confirm: bool = False) -> Report: ...
