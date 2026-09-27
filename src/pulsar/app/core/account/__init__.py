"""The accounts pulsar posts as: the ``provider:handle`` aliases that name them
(``aliases``), the registry of their state and bound identities
(``registry``), and the encrypted credential store behind it (``store``).
"""

from __future__ import annotations

from .aliases import alias_provider, normalize_alias
from .registry import (
    ACTIVE,
    LEGACY_NEEDS_ALIAS,
    REAUTH_REQUIRED,
    REVOKED,
    Account,
    AccountRegistry,
    MigrationResult,
    canonical_alias,
    check_handle,
    expected_handles,
    require_expected,
)
from .store import (
    REFRESH_LOCK_WAIT_SECONDS,
    CredentialConflict,
    CredentialStore,
    FernetFileStore,
    TokenBundle,
    home_command,
    login_command,
)

__all__ = [
    # aliases
    "alias_provider",
    "normalize_alias",
    # registry
    "ACTIVE",
    "LEGACY_NEEDS_ALIAS",
    "REAUTH_REQUIRED",
    "REVOKED",
    "Account",
    "AccountRegistry",
    "canonical_alias",
    "check_handle",
    "expected_handles",
    "MigrationResult",
    "require_expected",
    # store
    "REFRESH_LOCK_WAIT_SECONDS",
    "CredentialConflict",
    "CredentialStore",
    "FernetFileStore",
    "home_command",
    "login_command",
    "TokenBundle",
]
