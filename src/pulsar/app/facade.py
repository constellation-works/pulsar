"""``LocalApp``: the ``App`` over one home on this host, and one transport.

The entry point (``pulsar.main``) builds one from what it read of the process
(the environment, the cwd, the home) and hands it to a front end; the front
end calls it and never constructs anything below. Each verb builds the
``Runtime`` it needs and hands it to ``ops`` or ``health``. Each verb returns a
report ``(payload, exit_code)``: 0 settled and healthy, 1 not. Checks on how a
verb was invoked (a missing flag, a missing ``--confirm``) are the front end's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from pulsar.app.core.account import AccountRegistry, load_client_id
from pulsar.app.core.channels.x import PROVIDER
from pulsar.internal.errors import PulsarError
from pulsar.internal.fs import Paths

from . import approvals, health, ops
from .interfaces import Report
from .login import login
from .runtime import LocalRuntime
from .settings import Settings, load_settings


class LocalApp:
    """The verbs over ``paths``.

    ``environ`` is the process environment (``PULSAR_CALLER``, Orbit's), ``cwd``
    where relative media paths start; ``transport`` replaces the network in tests.
    """

    def __init__(
        self,
        paths: Paths,
        *,
        environ: Mapping[str, str],
        cwd: Path,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.paths = paths
        self.environ = environ
        self.cwd = cwd
        self.transport = transport

    @property
    def home(self) -> Path:
        return self.paths.home

    def runtime(
        self,
        *,
        read_only: bool = False,
        settings: Settings | None = None,
        media_base: Path | None = None,
    ) -> LocalRuntime:
        """A runtime over this home: the home's settings unless ``settings``,
        media paths from ``media_base`` else the cwd."""
        return LocalRuntime(
            self.paths,
            load_settings(self.paths) if settings is None else settings,
            environ=self.environ,
            media_base=self.cwd if media_base is None else media_base,
            transport=self.transport,
            read_only=read_only,
        )

    # -- accounts ---------------------------------------------------------------------

    def default_account(self) -> str | None:
        """``default_account`` from config.toml."""
        return load_settings(self.paths).default_account

    def remembered_client_id(self) -> str | None:
        """The X app client id remembered from the last login."""
        return load_client_id(self.paths, PROVIDER)

    def migrate_legacy_quietly(self) -> str | None:
        """First-use migration of a phase 1 home before an account write. A
        problem there must not block the write: it comes back as the notice to
        print, as does a migration that happened."""
        try:
            result = AccountRegistry(self.paths).migrate_legacy(load_settings(self.paths))
        except PulsarError as exc:
            return f"legacy credentials not migrated [{exc.code}]: {exc.message}"
        if result.state == "migrated":
            return f"migrated the legacy credentials to {result.alias}"
        return None

    def login(self, alias: str, client_id: str, *, open_browser: bool) -> Report:
        """Bind ``alias``: OAuth 2.0 PKCE in a browser, then verify the handle."""
        account = login(
            self.paths, load_settings(self.paths), alias, client_id, open_browser=open_browser
        )
        # login() asked X with the new token before storing it, so this is live proof.
        return {
            "alias": account.alias,
            "account": {"user_id": account.provider_user_id, "username": account.handle},
            "account_source": "live",
            "verified": True,
            "scope": " ".join(account.scopes) or None,
            "home": str(self.home),
        }, 0

    async def auth_status(self, *, account: str | None = None, live: bool = False) -> Report:
        # Only ``live`` may migrate, refresh and record; the offline report changes nothing.
        async with self.runtime(read_only=not live) as rt:
            return await health.auth_report(rt, account=account, live=live)

    def attention(self, entry: dict[str, Any]) -> str | None:
        """The remedy to show for one ``auth_status`` entry, if it needs one."""
        return health.attention(entry, self.home)

    def logout(self, account: str | None = None) -> Report:
        """Delete the account's tokens; the account is kept as revoked."""
        registry = AccountRegistry(self.paths)
        bound = registry.resolve(account, load_settings(self.paths))
        had_tokens = registry.store(bound.alias).exists()
        registry.logout(bound.alias)
        return {
            "alias": bound.alias,
            "home": str(self.home),
            "tokens_removed": had_tokens,
            "status": "revoked",
        }, 0

    def auth_migrate(self, *, account: str | None = None, confirm: bool = False) -> Report:
        """Report what moving phase 1 credentials would do; ``confirm`` makes it."""
        registry, settings = AccountRegistry(self.paths), load_settings(self.paths)
        if confirm:
            result = registry.migrate_legacy(settings, account)
        else:
            result = registry.legacy_status(settings, account)
        out = {
            "applied": confirm,
            "state": result.state,
            "alias": result.alias,
            "adopted": list(result.adopted),
            "message": result.message
            or {
                "migrated": f"legacy credentials are now {result.alias}; run "
                f"`pulsar auth status --live --account {result.alias}` to prove the binding",
                "none": "no legacy credentials to migrate",
            }.get(result.state),
            "home": str(self.home),
        }
        done = ("migrated", "none") if confirm else ("pending", "none")
        return out, 0 if result.state in done else 1

    # -- operator verbs ---------------------------------------------------------------

    def status(self, *, account: str | None = None) -> Report:
        return ops.budget_report(self.runtime(read_only=True), account=account)

    def history(
        self, *, account: str | None = None, limit: int = ops.HISTORY_LIMIT_DEFAULT
    ) -> Report:
        return ops.history_report(self.runtime(read_only=True), account=account, limit=limit)

    async def validate(self, plan: Path, *, account: str | None = None) -> Report:
        async with self.runtime(read_only=True) as rt:
            return await ops.validate_report(rt, plan, account=account)

    async def publish(
        self,
        plan: Path,
        *,
        account: str | None = None,
        idempotency_key: str | None = None,
        caller: str | None = None,
        confirm: bool = False,
    ) -> Report:
        # Without ``confirm`` it only validates: read-only, nothing written.
        async with self.runtime(read_only=not confirm) as rt:
            return await ops.publish_report(
                rt,
                plan,
                account=account,
                idempotency_key=idempotency_key,
                caller=caller,
                confirm=confirm,
            )

    def approve_preview(
        self,
        plan: Path,
        *,
        account: str | None = None,
        ttl: timedelta | None = None,
        workspace: Path | None = None,
    ) -> Report:
        rt = self._for_plans_in(workspace, read_only=True)
        return approvals.preview(rt, plan, account=account, ttl=ttl)

    def approve(
        self,
        plan: Path,
        *,
        expect: Mapping[str, str],
        account: str | None = None,
        ttl: timedelta | None = None,
        workspace: Path | None = None,
    ) -> Report:
        """Record the approval a human confirmed at a terminal (the CLI checks that)."""
        who = self.environ.get("USER") or self.environ.get("LOGNAME") or "unknown"
        return approvals.approve(
            self._for_plans_in(workspace, read_only=False),
            plan,
            account=account,
            ttl=ttl,
            approved_by=f"human:{who}",
            expect=expect,
        )

    def _for_plans_in(self, workspace: Path | None, *, read_only: bool) -> LocalRuntime:
        """A runtime whose plan media resolve against, and stay inside,
        ``workspace``, as the Orbit plugin's do; else the cwd and the
        configured roots."""
        if workspace is None:
            return self.runtime(read_only=read_only)
        root = workspace.resolve()
        settings = replace(load_settings(self.paths), media_roots=(root,))
        return self.runtime(read_only=read_only, settings=settings, media_base=root)

    def approvals(self, *, account: str | None = None, limit: int = 20) -> Report:
        return approvals.listing(self.runtime(read_only=True), account=account, limit=limit)

    def revoke(self, approval_id: int) -> Report:
        return approvals.revoke(self.runtime(), approval_id)

    async def reconcile(self, *, account: str | None = None) -> Report:
        async with self.runtime() as rt:
            return await ops.reconcile_report(rt, account=account)

    async def import_posted(
        self, source: Path, *, account: str | None = None, confirm: bool = False
    ) -> Report:
        async with self.runtime(read_only=not confirm) as rt:
            return await ops.import_report(rt, source, account=account, confirm=confirm)

    def migrate(self, *, confirm: bool = False) -> Report:
        return ops.migrate_report(self.runtime(read_only=not confirm), confirm=confirm)
