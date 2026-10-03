"""Binding a login to an account.

The OAuth steps are the provider's and store nothing: ``channels.x.auth``
for X (consent, callback, code exchange, ``/2/users/me``), the provider's
``AuthFlow`` from ``runtime.login_flow`` for the rest (Bluesky: atproto OAuth,
whose identity is the token's ``sub`` DID and the handle it resolves to).
This module decides what the result becomes: it refuses a token for another
handle than the alias's or its ``expected_handle`` (``account_mismatch``,
nothing stored), binds the bundle in the account registry, and records the
client id.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from pulsar.app.core.account import (
    Account,
    AccountRegistry,
    AccountSettings,
    alias_provider,
    canonical_alias,
    check_handle,
    save_client_id,
)
from pulsar.app.core.channels.contract import Authorized
from pulsar.app.core.channels.credentials import TokenBundle
from pulsar.app.core.channels.loopback import notify_stderr
from pulsar.app.core.channels.x import PROVIDER, auth, callback_url
from pulsar.internal.errors import INVALID_ARGUMENT, PulsarError
from pulsar.internal.fs import Paths

from .runtime import login_flow


def complete_login(
    paths: Paths,
    settings: AccountSettings,
    alias: str,
    client_id: str,
    bundle: TokenBundle,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Account:
    """Bind a freshly exchanged X ``bundle`` as ``alias``, if it really is that account.

    ``account_mismatch`` (naming both handles) when X says the token belongs
    to another handle than the alias's or the configured ``expected_handle``:
    nothing is stored, not even the client id. Otherwise the bundle becomes a
    new binding of ``alias`` under its refresh lock, with a verified row.
    """
    alias = require_x_alias(alias)
    identity = auth.fetch_identity(bundle, transport=transport)
    return bind_login(
        paths,
        settings,
        alias,
        Authorized(
            bundle=bundle, identity=identity, client_id=client_id, redirect_uri=callback_url()
        ),
    )


def bind_login(
    paths: Paths, settings: AccountSettings, alias: str, authorized: Authorized
) -> Account:
    """Bind what a provider's login handed back as ``alias``, if it is that account.

    ``authorized.identity`` came from the provider, asked about the new token.
    A handle other than the alias's or the configured ``expected_handle`` (or
    none) is ``account_mismatch`` and nothing is stored, not even the client id.
    """
    check_handle(alias, authorized.identity.handle or None, settings)
    account = AccountRegistry(paths).bind(alias, authorized.bundle, authorized.identity, settings)
    save_client_id(
        paths, alias_provider(alias), authorized.client_id, redirect_uri=authorized.redirect_uri
    )
    return account


def require_x_alias(alias: str) -> str:
    """``alias`` in canonical form, refused unless it names an X account."""
    canonical = canonical_alias(alias)
    if alias_provider(canonical) != PROVIDER:
        raise PulsarError(
            INVALID_ARGUMENT,
            f"{canonical} is not an X account; X logins bind x:<handle>",
            detail={"account": canonical},
        )
    return canonical


def default_client_id(provider: str) -> str | None:
    """The client id a ``provider`` login uses when nothing names one: Bluesky's
    loopback development client; None for X, whose app id a human must give."""
    return None if provider == PROVIDER else login_flow(provider).default_client_id()


def login(
    paths: Paths,
    settings: AccountSettings,
    alias: str,
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
    notify: Callable[[str], None] = notify_stderr,
) -> Account:
    """``pulsar auth login --account <alias>``: consent in a browser, verify, bind.

    ``notify`` shows the human the consent URL (default: stderr).
    """
    canonical = canonical_alias(alias)
    provider = alias_provider(canonical)
    if provider != PROVIDER:
        # Refuse a provider without a login before sending the human anywhere.
        flow = login_flow(provider, transport=transport)
        handle = canonical.partition(":")[2]
        authorized = flow.authorize(handle, client_id, open_browser=open_browser, notify=notify)
        return bind_login(paths, settings, canonical, authorized)
    bundle = auth.authorize(
        client_id, open_browser=open_browser, transport=transport, notify=notify
    )
    return complete_login(paths, settings, canonical, client_id, bundle, transport=transport)
