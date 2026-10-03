"""The credentials a channel is handed: the OAuth token bundle, the
``CredentialStore`` it is kept behind, the conflict a compare-and-swap save
raises, and ``ChannelClient``, what every provider's client offers over them.
A channel client refreshes through the store under its refresh lock;
``account.store.FernetFileStore`` is the implementation the app builds.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from pulsar.internal.errors import INTERNAL, PulsarError
from pulsar.internal.fs import obj

# Longer than one token POST (the HTTP timeout) so a waiter outlasts a live refresher.
REFRESH_LOCK_WAIT_SECONDS = 45.0


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str | None
    expires_at: float  # epoch seconds
    scope: str
    client_id: str
    token_type: str = "bearer"  # "DPoP" for a token bound to a key (Bluesky)
    # Minted per login, carried across refreshes; None for bundles saved before it existed.
    binding_id: str | None = None

    def expires_within(self, seconds: float) -> bool:
        return time.time() + seconds >= self.expires_at

    @classmethod
    def from_token_response(
        cls, data: object, *, client_id: str, now: float | None = None
    ) -> TokenBundle:
        """Raises ``KeyError``/``ValueError`` on a response without a usable access token."""
        fields = obj(data)
        now = time.time() if now is None else now
        access = fields["access_token"]
        if not isinstance(access, str) or not access:
            raise ValueError("token response has no access_token")
        refresh = fields.get("refresh_token")
        # No ``expires_in`` means we do not know when the token dies. Rather
        # than invent a lifetime, treat it as expiring now: the
        # next call refreshes first, which costs one token POST at worst.
        expires_in = fields.get("expires_in")
        return cls(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) and refresh else None,
            expires_at=now + float(expires_in) if expires_in is not None else now,
            scope=str(fields.get("scope", "")),
            client_id=client_id,
            token_type=str(fields.get("token_type", "bearer")),
        )


class CredentialConflict(PulsarError):
    """A compare-and-swap save found a different bundle than the caller expected.

    Every refresher in pulsar catches this and adopts the stored bundle, so it
    reaches a caller only from a call site that forgot to; that is a bug
    (``internal``). It is still retryable: another writer saved a newer bundle,
    and a repeat call reloads the store and uses it.
    """

    def __init__(self) -> None:
        super().__init__(
            INTERNAL,
            "CredentialConflict: the stored credential changed during refresh (a writer "
            "bypassed the refresh lock); retrying the call uses the newer bundle",
            retryable=True,
        )


class CredentialStore(Protocol):
    """What pulsar needs from wherever the token bundle is kept."""

    def exists(self) -> bool: ...

    def reauth_hint(self) -> str:
        """What a human runs to bind this store's account again, for error messages."""
        ...

    def load(self) -> TokenBundle | None:
        """The stored bundle, or None when this host is not authorized.

        Raises ``insecure_storage`` instead of returning None when a bundle is
        there but stored unsafely, and ``credentials_unreadable`` when it is
        there but cannot be decrypted or parsed: None sends the operator to
        re-login, which fixes neither.
        """
        ...

    def save(self, bundle: TokenBundle, *, expected_previous: TokenBundle | None = None) -> None:
        """Store ``bundle``; with ``expected_previous``, only if that is what is stored now.

        A mismatch raises ``CredentialConflict`` and leaves the store untouched.
        """
        ...

    def rebind(
        self, bundle: TokenBundle, *, on_bound: Callable[[TokenBundle], object] | None = None
    ) -> TokenBundle:
        """Store a freshly issued bundle as a new binding, under the refresh lock.

        Mints the ``binding_id`` and drops the cached identity; ``on_bound``
        (say, the registry update) runs with the lock still held. Returns the
        bundle as stored.
        """
        ...

    def clear(self, *, on_cleared: Callable[[], object] | None = None) -> None:
        """Forget the binding (tokens and cached identity), under the refresh lock.

        ``on_cleared`` runs with the lock still held.
        """
        ...

    def refresh_lock(self, timeout: float) -> AbstractAsyncContextManager[None]:
        """Exclusive across processes; not getting it within ``timeout`` is ``lock_timeout``."""
        ...


class ChannelClient(Protocol):
    """One account's authenticated client, whatever its provider: what the app
    keeps per account, refreshes for a live health check, and closes."""

    @property
    def store(self) -> CredentialStore: ...

    async def refresh(self, bundle: TokenBundle) -> TokenBundle:
        """Replace ``bundle`` with a working one, under the store's refresh lock."""
        ...

    async def aclose(self) -> None: ...
