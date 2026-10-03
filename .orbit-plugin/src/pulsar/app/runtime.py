"""Composition: the one place that joins configuration, storage, the ledger and the channels.

``LocalRuntime``, the ``Runtime`` over one home, is built from resolved inputs
(the home, the settings, the process environment, the directory relative
media paths start from) that the entry point (``pulsar.main``) read once and
``LocalApp`` hands down; nothing here or below reads the environment, the
cwd or ``$HOME`` itself.

It is also the provider -> channel factory: ``client_for`` builds an
account's client by the provider its alias names, and ``channel`` binds
that client to the account as a ``Channel``; ``login_flow`` is the
provider's human login. Everything else reaches a provider only through
those three.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from collections.abc import AsyncGenerator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from pulsar.app.core.account import (
    ACTIVE,
    REAUTH_REQUIRED,
    REVOKED,
    Account,
    AccountRegistry,
    FernetFileStore,
    alias_provider,
    login_command,
    require_expected,
)
from pulsar.app.core.channels import bluesky, x
from pulsar.app.core.channels.bluesky import BlueskyChannel, BlueskyClient, BlueskyLogin
from pulsar.app.core.channels.contract import AuthFlow, Channel
from pulsar.app.core.channels.x import XChannel, XClient
from pulsar.app.core.engagement import Reader
from pulsar.app.core.ledger import SqliteLedger
from pulsar.app.core.publishing import Bound, Plan, Publisher
from pulsar.internal.errors import (
    INVALID_ARGUMENT,
    SECRET_DETECTED,
    UNSUPPORTED,
    AuthExpired,
    PulsarError,
)
from pulsar.internal.fs import Paths
from pulsar.internal.guard import redact, scan_for_secrets

from .settings import Settings
from .writelog import WriteLog

# The operator's default audit label for writes a caller does not label.
CALLER_ENV = "PULSAR_CALLER"
# Set by Orbit for a plugin backend: the only directory the sandbox can write.
PLUGIN_STATE_ENV = "ORBIT_PLUGIN_STATE"

log = logging.getLogger(__name__)

# An account's client, by its provider.
type Client = XClient | BlueskyClient


class RedactingFilter(logging.Filter):
    """Masks credential shapes in every log line before a handler writes it:
    stderr is kept by MCP hosts and Orbit's run logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        masked = redact(message)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if masked != message or record.args:
            record.msg, record.args = masked, None
        return True


def configure_logging(level: int = logging.WARNING) -> None:
    """Log to stderr (stdout carries the payload), through ``RedactingFilter``."""
    root = logging.getLogger()
    if any(isinstance(h, _PulsarHandler) for h in root.handlers):
        return
    handler = _PulsarHandler(sys.stderr)
    handler.addFilter(RedactingFilter())
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)


class _PulsarHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marks the handler ``configure_logging`` installed, so it is installed once."""


def login_flow(provider: str, *, transport: httpx.BaseTransport | None = None) -> AuthFlow:
    """``provider``'s human login; ``unsupported`` for a provider pulsar cannot log in to.

    X's login predates ``AuthFlow`` and is ``app.login``'s own.
    """
    if provider == bluesky.PROVIDER:
        return BlueskyLogin(transport=transport)
    raise PulsarError(UNSUPPORTED, f"pulsar has no login for provider {provider!r}")


def default_paths(environ: Mapping[str, str], user_home: Path) -> Paths:
    """The home layout for this process, from the environment the entry point read.

    Under Orbit (``ORBIT_PLUGIN_STATE`` set) the home is
    ``$ORBIT_PLUGIN_STATE/home``: the plugin sandbox can write only its state.
    Otherwise ``PULSAR_HOME``, else ``<user_home>/.config/pulsar``.
    """
    state = environ.get(PLUGIN_STATE_ENV)
    if state:
        return Paths(home=Path(state) / "home", user_home=user_home)
    return Paths.from_environ(environ, user_home)


class LocalRuntime:
    """Everything the verbs and tools need: ``App.runtime`` builds one per server or call.

    Accounts are resolved per call, not at start-up: a login, logout or
    migration while the server runs takes effect on the next call. Each
    account gets its own client (``XClient``, ``BlueskyClient``) over its own
    credential store, so accounts refresh independently.

    ``read_only`` is for the reports: the ledger is opened
    read-only and nothing is migrated, so a report changes nothing on disk.
    """

    def __init__(
        self,
        paths: Paths,
        settings: Settings,
        *,
        environ: Mapping[str, str],
        media_base: Path,
        transport: httpx.AsyncBaseTransport | None = None,
        read_only: bool = False,
        **client_kwargs: Any,
    ) -> None:
        self.read_only = read_only
        self.environ = environ
        self.paths = paths
        self.settings = settings
        self.registry = AccountRegistry(self.paths)
        self.log = WriteLog(self.paths)
        self.ledger = SqliteLedger(self.paths, export=self.log.export, read_only=read_only)
        self.publisher = Publisher(
            ledger=self.ledger,
            settings=self.settings,
            deny=(self.paths.home,),
            # Relative media paths are the caller's: the process's cwd, or
            # the Orbit workspace.
            media_base=media_base,
        )
        self.reader = Reader(ledger=self.ledger, settings=self.settings)
        self._transport = transport
        self._client_kwargs = client_kwargs
        self._clients: dict[str, Client] = {}
        self._migrated = False

    async def __aenter__(self) -> LocalRuntime:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def caller(self, explicit: str | None, default: str = "unknown") -> str:
        """The audit label for a write: the argument, else ``PULSAR_CALLER``, else ``default``.

        Self-asserted and recorded as such; never identity.
        """
        caller = explicit or self.environ.get(CALLER_ENV) or default
        if scan_for_secrets(caller):
            raise PulsarError(SECRET_DETECTED, "caller looks like it contains a credential")
        return caller

    # -- accounts -----------------------------------------------------------

    def account(self, alias: str | None = None) -> Account:
        """The account a call acts as (``AccountRegistry.resolve``).

        The first call of a read-write runtime migrates a phase 1
        single-account home, if there is one; a read-only one leaves it.
        """
        if not self._migrated and not self.read_only:
            self.registry.migrate_legacy(self.settings)
            self._migrated = True
        return self.registry.resolve(alias, self.settings)

    def client_for(self, alias: str) -> Client:
        """``alias``'s client, built once per runtime for the provider the alias names.

        ``unsupported`` for a provider pulsar has no channel for.
        """
        client = self._clients.get(alias)
        if client is None:
            provider = alias_provider(alias)
            store = self.registry.store(alias)
            if provider == x.PROVIDER:
                client = XClient(store, transport=self._transport, **self._client_kwargs)
            elif provider == bluesky.PROVIDER:
                client = BlueskyClient(store, transport=self._transport, **self._client_kwargs)
            else:
                raise PulsarError(UNSUPPORTED, f"no channel for provider {provider!r}")
            self._clients[alias] = client
        return client

    @property
    def client(self) -> Client:
        """The default account's client."""
        return self.client_for(self.account().alias)

    @property
    def store(self) -> FernetFileStore:
        """The default account's credential store."""
        return self.registry.store(self.account().alias)

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await client.aclose()

    @contextlib.asynccontextmanager
    async def watch_expiry(self, alias: str) -> AsyncGenerator[None]:
        """Mark ``alias`` ``reauth_required`` when what runs inside ends in ``auth_expired``.

        The registry write takes a file lock, so it runs in a thread, off the
        loop other sessions share; a failure to record it is
        logged and never replaces the ``auth_expired`` the caller must see.
        """
        try:
            yield
        except AuthExpired:
            try:
                await asyncio.to_thread(self._mark_reauth_required, alias)
            except Exception:
                log.exception("accounts: could not mark %s reauth_required", alias)
            raise

    def _mark_reauth_required(self, alias: str) -> None:
        row = self.registry.accounts().get(alias)
        if row is not None and row.status == ACTIVE:
            self.registry.mark_status(alias, REAUTH_REQUIRED)

    async def identity(self, alias: str | None = None, *, live: bool = False) -> Account:
        """The account, with its identity trusted for the stored binding.

        Read from the registry row while that row describes the stored
        bundle's binding; otherwise (or when ``live``) asked of X and
        recorded. The lookup is pinned to the bundle read here, so a re-login
        that lands during it fails the lookup rather than recording the new
        login's identity under the old binding. The returned ``binding_id``
        is the binding the identity was checked for: ``writer`` sends with it.
        The lookup is the channel's ``whoami``, so it asks whichever provider
        the alias names.
        """
        # Registry and credential files are read under file locks: in a
        # thread, so a lock held by another process stalls this call only.
        account = await asyncio.to_thread(self.account, alias)
        name = account.alias
        async with self.watch_expiry(name):
            if account.status == REVOKED:
                raise AuthExpired(
                    f"{name} was logged out; a human runs `{login_command(self.paths.home, name)}`"
                )
            client = self.client_for(name)
            bundle = await asyncio.to_thread(client.store.load)
            if bundle is None:
                raise AuthExpired(
                    f"no credentials stored for {name}; a human runs "
                    f"`{login_command(self.paths.home, name)}`"
                )
            if not live and (trusted := self.registry.trusted_identity(account, bundle)):
                return replace(
                    account,
                    handle=trusted.handle,
                    provider_user_id=trusted.provider_user_id,
                    binding_id=bundle.binding_id,
                )
            pinned = client.pinned(bundle.binding_id)
            found = await self.channel(name, user_id="", handle="", client=pinned).whoami()
        await asyncio.to_thread(self.registry.mark_verified, name, found, bundle.binding_id)
        return replace(
            account,
            handle=found.handle,
            provider_user_id=found.provider_user_id,
            binding_id=bundle.binding_id,
        )

    async def whoami(self, alias: str | None = None, *, live: bool = False) -> dict[str, str]:
        """``{user_id, username}`` of the account: cached after the first call."""
        return me(await self.identity(alias, live=live))

    async def writer(self, alias: str | None) -> tuple[Account, XClient, dict[str, str]]:
        """The X account to write as with the single-request tools, its client and
        ledger identity (``checked``). Every other write goes through ``bound``.

        ``unsupported`` for an account of another provider.
        """
        account, client, found = await self.checked(alias)
        if not isinstance(client, XClient):
            raise PulsarError(
                UNSUPPORTED,
                f"{account.alias}: the single-post tools are X's; publish a plan instead",
            )
        return account, client, found

    async def checked(self, alias: str | None) -> tuple[Account, Client, dict[str, str]]:
        """The account to write as, its client and ledger identity.

        ``account_mismatch`` when the bound handle is not the alias's or the
        configured ``expected_handle``: checked before every write. The client
        is pinned to the binding that check was for, so a re-login between the
        check and the request sends nothing (``XClient.pinned``,
        ``BlueskyClient.pinned``).
        """
        account = await self.identity(alias)
        require_expected(account, self.settings)
        client = self.client_for(account.alias).pinned(account.binding_id)
        return account, client, me(account)

    # -- channels -------------------------------------------------------------

    def channel(
        self, alias: str, *, user_id: str, handle: str, client: Client | None = None
    ) -> Channel:
        """``alias``'s channel, over ``client`` (a pinned one, for writes) when given."""
        client = client or self.client_for(alias)
        if isinstance(client, XClient):
            return XChannel(client, user_id=user_id, handle=handle)
        return BlueskyChannel(client, did=user_id, handle=handle)

    def offline_bound(self, alias: str) -> Bound:
        """``alias`` bound for offline validation: no credentials needed, no network."""
        row = self.registry.accounts().get(alias)
        handle = (row.handle if row else None) or alias.partition(":")[2]
        user_id = (row.provider_user_id if row else None) or ""
        return Bound(
            alias=alias,
            provider=alias_provider(alias),
            user_id=user_id,
            handle=handle,
            channel=self.channel(alias, user_id=user_id, handle=handle),
        )

    async def bound(self, alias: str | None) -> Bound:
        """The account to publish as, identity checked (``checked``), with its channel
        pinned to the checked binding."""
        account, client, me = await self.checked(alias)
        return Bound(
            alias=account.alias,
            provider=account.provider,
            user_id=me["user_id"],
            handle=me["username"],
            channel=self.channel(
                account.alias, user_id=me["user_id"], handle=me["username"], client=client
            ),
        )

    def plan_targets(self, plan: Plan, account: str | None) -> tuple[Plan, list[str]]:
        """The plan bound to explicit accounts, and the ones this call acts for.

        A plan without accounts is bound to ``account`` (else the default), so
        its digest names who it is for. ``account`` on a plan that names
        accounts selects one of them.
        """
        if not plan.accounts:
            chosen = self.account(account).alias
            return plan.with_accounts((chosen,)), [chosen]
        if account is None:
            return plan, list(plan.accounts)
        chosen = self.account(account).alias
        if chosen not in plan.accounts:
            raise PulsarError(
                INVALID_ARGUMENT,
                f"{chosen} is not one of the plan's accounts",
                detail={"accounts": list(plan.accounts)},
            )
        return plan, [chosen]


def me(account: Account) -> dict[str, str]:
    return {"user_id": account.provider_user_id or "", "username": account.handle or ""}
