"""Binding an X login to an account.

``channels.x.auth`` runs the OAuth steps (consent, callback, code exchange,
``/2/users/me``) and stores nothing; this module decides what the result
becomes: it refuses a token for another handle than the alias's or its
``expected_handle`` (``account_mismatch``, nothing stored), binds the bundle
in the account registry, and records the client id.
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
from pulsar.app.core.channels.credentials import TokenBundle
from pulsar.app.core.channels.x import PROVIDER, auth, callback_url
from pulsar.internal.errors import INVALID_ARGUMENT, PulsarError
from pulsar.internal.fs import Paths


def complete_login(
    paths: Paths,
    settings: AccountSettings,
    alias: str,
    client_id: str,
    bundle: TokenBundle,
    *,
    transport: httpx.BaseTransport | None = None,
) -> Account:
    """Bind a freshly exchanged ``bundle`` as ``alias``, if it really is that account.

    ``account_mismatch`` (naming both handles) when X says the token belongs
    to another handle than the alias's or the configured ``expected_handle``:
    nothing is stored, not even the client id. Otherwise the bundle becomes a
    new binding of ``alias`` under its refresh lock, with a verified row.
    """
    alias = require_x_alias(alias)
    identity = auth.fetch_identity(bundle, transport=transport)
    check_handle(alias, identity.handle, settings)
    account = AccountRegistry(paths).bind(alias, bundle, identity, settings)
    save_client_id(paths, PROVIDER, client_id, redirect_uri=callback_url())
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


def login(
    paths: Paths,
    settings: AccountSettings,
    alias: str,
    client_id: str,
    *,
    open_browser: bool = True,
    transport: httpx.BaseTransport | None = None,
    notify: Callable[[str], None] = auth.notify_stderr,
) -> Account:
    """``pulsar auth login --account x:<handle>``: consent in a browser, verify, bind.

    ``notify`` shows the human the consent URL (default: stderr).
    """
    require_x_alias(alias)  # refuse a bad alias before sending the human to X
    bundle = auth.authorize(
        client_id, open_browser=open_browser, transport=transport, notify=notify
    )
    return complete_login(paths, settings, alias, client_id, bundle, transport=transport)
