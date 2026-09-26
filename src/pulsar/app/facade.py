"""``App``: pulsar's verbs, bound to one home and one transport.

The entry point (``pulsar.main``) builds one and hands it to a front end; the
front end calls it and never constructs anything below. Each verb returns a
report ``(payload, exit_code)``: 0 settled and healthy, 1 not. Checks on how a
verb was invoked (a missing flag, a missing ``--confirm``) are the front end's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from pulsar.core import AccountRegistry, Paths, PulsarError, load_settings
from pulsar.providers.x import load_client_id, login

from . import health, ops
from .runtime import Runtime

Report = tuple[dict[str, Any], int]


class App:
    """The verbs over ``paths``; ``transport`` replaces the network in tests."""

    def __init__(self, paths: Paths, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.paths = paths
        self.transport = transport

    @property
    def home(self) -> Path:
        return self.paths.home

    def runtime(self) -> Runtime:
        """A runtime over this home, for a long-running server."""
        return Runtime(self.paths)

    # -- accounts ---------------------------------------------------------------------

    def default_account(self) -> str | None:
        """``default_account`` from config.toml."""
        return load_settings(self.paths).default_account

    def remembered_client_id(self) -> str | None:
        """The X app client id remembered from the last login."""
        return load_client_id(self.paths)

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

    async def auth_status(self, *, account: str | None, live: bool) -> Report:
        return await health.auth_report(
            self.paths, account=account, live=live, transport=self.transport
        )

    def attention(self, entry: dict[str, Any]) -> str | None:
        """The remedy to show for one ``auth_status`` entry, if it needs one."""
        return health.attention(entry, self.home)

    def logout(self, account: str | None) -> Report:
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

    def auth_migrate(self, *, account: str | None, confirm: bool) -> Report:
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

    def status(self, *, account: str | None) -> Report:
        return ops.budget_report(self.paths, account=account)

    def history(self, *, account: str | None, limit: int) -> Report:
        return ops.history_report(self.paths, account=account, limit=limit)

    async def validate(self, plan: Path, *, account: str | None) -> Report:
        return await ops.validate_report(self.paths, plan, account=account)

    async def publish(
        self,
        plan: Path,
        *,
        account: str | None,
        idempotency_key: str | None,
        caller: str | None,
        confirm: bool,
    ) -> Report:
        return await ops.publish_report(
            self.paths,
            plan,
            account=account,
            idempotency_key=idempotency_key,
            caller=caller,
            confirm=confirm,
            transport=self.transport,
        )

    async def reconcile(self, *, account: str | None) -> Report:
        return await ops.reconcile_report(self.paths, account=account, transport=self.transport)

    async def import_posted(self, source: Path, *, account: str | None, confirm: bool) -> Report:
        return await ops.import_report(
            self.paths, source, account=account, confirm=confirm, transport=self.transport
        )

    def migrate(self, *, confirm: bool) -> Report:
        return ops.migrate_report(self.paths, confirm=confirm)
