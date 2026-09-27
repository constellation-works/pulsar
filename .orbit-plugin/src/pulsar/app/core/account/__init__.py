"""The accounts pulsar posts as: the ``provider:handle`` aliases that name them
(``aliases``), the registry of their state and bound identities
(``registry``), and the encrypted credential store behind it (``store``).
"""

from __future__ import annotations

from .aliases import alias_provider, normalize_alias
from .clients import load_client_id, save_client_id
from .registry import (
    ACTIVE,
    LEGACY_NEEDS_ALIAS,
    REAUTH_REQUIRED,
    REVOKED,
    Account,
    AccountConfig,
    AccountRegistry,
    AccountSettings,
    MigrationResult,
    canonical_alias,
    check_handle,
    expected_handles,
    require_expected,
)
from .store import FernetFileStore, home_command, login_command

__all__ = [
    # aliases
    "alias_provider",
    "normalize_alias",
    # clients
    "load_client_id",
    "save_client_id",
    # registry
    "ACTIVE",
    "LEGACY_NEEDS_ALIAS",
    "REAUTH_REQUIRED",
    "REVOKED",
    "Account",
    "AccountConfig",
    "AccountRegistry",
    "AccountSettings",
    "canonical_alias",
    "check_handle",
    "expected_handles",
    "MigrationResult",
    "require_expected",
    # store
    "FernetFileStore",
    "home_command",
    "login_command",
]
