"""The account registry: several provider accounts in one pulsar home.

Accounts are keyed by alias, ``provider:handle`` in canonical form
(``x:constworks``). ``accounts.json`` in the home holds one row per alias::

    alias, provider, provider_user_id, handle, scopes,
    status (active | reauth_required | revoked),
    bound_at, binding_id, verified_at

Credentials live beside it, per account: ``accounts/<slug>/tokens.enc`` and
``accounts/<slug>/refresh.lock``, encrypted under the one root ``key``
(``FernetFileStore.for_account``). The registry holds no secret.

The row is also the identity cache that replaces phase 1's ``whoami.json``:
its ``handle`` and ``provider_user_id`` are trusted only while its
``binding_id`` equals the stored bundle's, so a lookup that raced a re-login
(and so recorded the old account under the old binding) is never trusted.

``require_expected`` is the check that replaced the manual "must be
@constworks" runbook step: on 2026-09-16 posts landed on the wrong account
because the wrong token was stored. A write goes out only when the bound
handle equals the alias's handle and the configured ``expected_handle``.

Lock order, everywhere: the legacy root refresh lock, then an account's
refresh lock, then ``accounts.lock``. ``accounts.lock`` is only ever held for
one short read-modify-write of ``accounts.json``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapter import Identity
from .errors import (
    ACCOUNT_MISMATCH,
    INVALID_ARGUMENT,
    INVALID_CONFIG,
    UNKNOWN_ACCOUNT,
    AuthExpired,
    PulsarError,
)
from .fsutil import FILE_MODE, ensure_private_dir, require_private, write_private_atomic
from .jsonx import as_list, as_object
from .paths import Paths, account_slug, alias_from_slug
from .plan import alias_provider, normalize_alias
from .settings import Settings
from .store import REFRESH_LOCK_WAIT_SECONDS, FernetFileStore, TokenBundle

ACTIVE = "active"
REAUTH_REQUIRED = "reauth_required"
REVOKED = "revoked"
STATUSES = frozenset({ACTIVE, REAUTH_REQUIRED, REVOKED})
REGISTRY_VERSION = 1

# The phase 1 layout held one X account at the home root.
LEGACY_PROVIDER = "x"
LEGACY_NEEDS_ALIAS = (
    "legacy credentials need an alias: run `pulsar auth migrate --account x:<handle>`"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class Account:
    alias: str
    provider: str
    handle: str | None = None  # the bound handle, lower-case, once known
    provider_user_id: str | None = None
    scopes: tuple[str, ...] = ()
    status: str = ACTIVE
    bound_at: str | None = None
    binding_id: str | None = None  # the binding handle/provider_user_id describe
    verified_at: str | None = None  # last time the provider confirmed the identity

    @property
    def alias_handle(self) -> str:
        return self.alias.partition(":")[2]

    def to_json(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "provider": self.provider,
            "provider_user_id": self.provider_user_id,
            "handle": self.handle,
            "scopes": list(self.scopes),
            "status": self.status,
            "bound_at": self.bound_at,
            "binding_id": self.binding_id,
            "verified_at": self.verified_at,
        }

    @classmethod
    def from_json(cls, alias: str, raw: object) -> Account:
        row = as_object(raw)
        status = row.get("status", ACTIVE) if row is not None else None
        if row is None or status not in STATUSES:
            raise _corrupt(f"row {alias!r} is malformed")
        return cls(
            alias=alias,
            provider=alias_provider(alias),
            handle=_opt_str(row.get("handle")),
            provider_user_id=_opt_str(row.get("provider_user_id")),
            scopes=tuple(s for s in as_list(row.get("scopes")) if isinstance(s, str)),
            status=str(status),
            bound_at=_opt_str(row.get("bound_at")),
            binding_id=_opt_str(row.get("binding_id")),
            verified_at=_opt_str(row.get("verified_at")),
        )


@dataclass(frozen=True)
class MigrationResult:
    """What ``migrate_legacy`` did.

    ``state``: ``none`` (nothing to migrate), ``migrated`` (the root bundle
    is now ``alias``), ``needs_alias`` (left in place: no alias could be
    named), ``ignored`` (left in place because accounts are already
    registered and none was named). ``adopted`` lists account directories
    that held credentials but had no row (a crash between moving a bundle
    and recording it) and were recorded.
    """

    state: str
    alias: str | None = None
    message: str | None = None
    adopted: tuple[str, ...] = field(default=())


def _corrupt(problem: str) -> PulsarError:
    return PulsarError(
        INVALID_CONFIG,
        f"accounts.json: {problem}; restore it from backup, or remove it and re-run "
        "`pulsar auth login --account provider:handle` for each account",
    )


def canonical_alias(value: str) -> str:
    """``X:@ConstWorks`` -> ``x:constworks``; ``invalid_argument`` if malformed or unstorable."""
    try:
        alias = normalize_alias(value)
    except PulsarError as exc:
        raise PulsarError(INVALID_ARGUMENT, exc.message, detail={"account": value}) from exc
    account_slug(alias)
    return alias


def expected_handles(alias: str, settings: Settings) -> tuple[str, ...]:
    """The handles an account bound as ``alias`` must have: the alias's own,
    and the configured ``expected_handle`` when it names another."""
    wanted = [alias.partition(":")[2]]
    config = settings.account_config(alias)
    if config is not None and config.expected_handle and config.expected_handle not in wanted:
        wanted.append(config.expected_handle)
    return tuple(wanted)


def check_handle(alias: str, bound_handle: str | None, settings: Settings) -> None:
    """``account_mismatch`` unless ``bound_handle`` is what ``alias`` must be bound to."""
    for want in expected_handles(alias, settings):
        if bound_handle is None or bound_handle.lower() != want:
            bound = f"@{bound_handle}" if bound_handle else "an unverified account"
            raise PulsarError(
                ACCOUNT_MISMATCH,
                f"{alias} must be bound to @{want}, but its credentials belong to {bound}; "
                f"nothing was written. A human re-runs `pulsar auth login --account {alias}` "
                f"and approves as @{want}",
                detail={"alias": alias, "expected_handle": want, "bound_handle": bound_handle},
            )


def require_expected(account: Account, settings: Settings) -> None:
    """Refuse (``account_mismatch``) to act as ``account`` unless its bound handle is right.

    Call it with the identity just trusted for the stored binding (see
    ``AccountRegistry.trusted_identity``), before any write.
    """
    check_handle(account.alias, account.handle, settings)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class AccountRegistry:
    """``accounts.json`` plus the per-account credential stores it names."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths

    def store(self, alias: str) -> FernetFileStore:
        return FernetFileStore.for_account(self.paths, alias)

    # -- file ---------------------------------------------------------------

    @contextlib.contextmanager
    def _lock(self) -> Generator[None]:
        ensure_private_dir(self.paths.home)
        fd = os.open(self.paths.accounts_lock, os.O_RDWR | os.O_CREAT, FILE_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read(self) -> dict[str, Account]:
        path = self.paths.accounts_file
        require_private(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        try:
            data = as_object(json.loads(raw))
        except ValueError as exc:
            raise _corrupt("not valid JSON") from exc
        rows = as_object(data.get("accounts")) if data is not None else None
        if rows is None:
            raise _corrupt("no `accounts` table")
        out: dict[str, Account] = {}
        for alias, row in rows.items():
            try:
                canonical = canonical_alias(alias)
            except PulsarError as exc:
                raise _corrupt(f"bad alias {alias!r}") from exc
            out[canonical] = Account.from_json(canonical, row)
        return out

    def _write(self, rows: dict[str, Account]) -> None:
        ensure_private_dir(self.paths.home)
        doc = {
            "version": REGISTRY_VERSION,
            "accounts": {alias: rows[alias].to_json() for alias in sorted(rows)},
        }
        write_private_atomic(
            self.paths.accounts_file, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
        )

    def _update(self, alias: str, change: Callable[[Account], Account]) -> Account | None:
        with self._lock():
            rows = self._read()
            current = rows.get(alias)
            if current is None:
                return None
            rows[alias] = change(current)
            self._write(rows)
            return rows[alias]

    # -- reading ------------------------------------------------------------

    def accounts(self) -> dict[str, Account]:
        """Every registered account (revoked ones too), by alias."""
        return self._read()

    def get(self, alias: str) -> Account | None:
        return self._read().get(canonical_alias(alias))

    def resolve(self, alias: str | None, settings: Settings) -> Account:
        """The account a call acts as.

        A named ``alias`` must be registered (``unknown_account``). With none:
        ``settings.default_account``, else the only account that is not
        revoked, else ``invalid_argument`` (several are bound; name one).
        With none bound at all, ``auth_expired``: a human has to log in.
        """
        rows = self._read()
        if alias is not None or settings.default_account is not None:
            wanted = canonical_alias(alias) if alias is not None else settings.default_account
            assert wanted is not None
            found = rows.get(wanted)
            if found is None:
                raise self._unknown(wanted, rows, from_default=alias is None)
            return found
        bound = [a for a in rows.values() if a.status != REVOKED]
        if len(bound) == 1:
            return bound[0]
        if bound:
            raise PulsarError(
                INVALID_ARGUMENT,
                "several accounts are bound; name one with `account` "
                "(or set default_account in config.toml)",
                detail={"accounts": sorted(a.alias for a in bound)},
            )
        if not rows and self.paths.token_file.exists():
            raise AuthExpired(LEGACY_NEEDS_ALIAS)
        raise AuthExpired(
            "no account is bound on this host; a human runs "
            "`pulsar auth login --account provider:handle`"
        )

    def _unknown(self, alias: str, rows: dict[str, Account], *, from_default: bool) -> PulsarError:
        known = sorted(rows)
        where = "default_account in config.toml names" if from_default else "no account"
        hint = (
            f"; {LEGACY_NEEDS_ALIAS}"
            if not rows and self.paths.token_file.exists()
            else f"; a human binds it with `pulsar auth login --account {alias}`"
        )
        return PulsarError(
            UNKNOWN_ACCOUNT,
            f"{where} {alias}, which is not registered on this host "
            f"(known: {', '.join(known) or 'none'}){hint}",
            detail={"account": alias, "known": known},
        )

    @staticmethod
    def trusted_identity(account: Account, bundle: TokenBundle) -> Identity | None:
        """The row's identity if it describes ``bundle``'s binding, else None."""
        if account.binding_id != bundle.binding_id:
            return None
        if account.handle is None or account.provider_user_id is None:
            return None
        return Identity(provider_user_id=account.provider_user_id, handle=account.handle)

    # -- writing ------------------------------------------------------------

    def put(self, account: Account) -> None:
        """Insert or replace ``account``'s row as given (no credential is touched)."""
        alias = canonical_alias(account.alias)
        with self._lock():
            rows = self._read()
            rows[alias] = replace(account, alias=alias, provider=alias_provider(alias))
            self._write(rows)

    def bind(
        self, alias: str, bundle: TokenBundle, identity: Identity, settings: Settings
    ) -> Account:
        """Store a freshly issued ``bundle`` as ``alias``, whose owner is ``identity``.

        ``identity`` must come from the provider, asked with ``bundle``'s own
        token. A handle that is not the alias's (or the configured
        ``expected_handle``) is ``account_mismatch`` and stores nothing.
        Otherwise the bundle becomes a new binding under the account's
        refresh lock, and the row is written before the lock is released.
        """
        alias = canonical_alias(alias)
        check_handle(alias, identity.handle, settings)
        bound: list[Account] = []

        def record(stored: TokenBundle) -> None:
            now = _now()
            with self._lock():
                rows = self._read()
                rows[alias] = Account(
                    alias=alias,
                    provider=alias_provider(alias),
                    handle=identity.handle.lower(),
                    provider_user_id=identity.provider_user_id,
                    scopes=tuple(stored.scope.split()),
                    status=ACTIVE,
                    bound_at=now,
                    binding_id=stored.binding_id,
                    verified_at=now,
                )
                self._write(rows)
                bound.append(rows[alias])

        self.store(alias).rebind(bundle, on_bound=record)
        return bound[0]

    def mark_verified(self, alias: str, identity: Identity, binding_id: str | None) -> None:
        """Record that the provider named ``identity`` for the binding ``binding_id``.

        Written even when it describes an older binding than the stored one
        (a lookup that raced a re-login): ``trusted_identity`` then declines it.
        The status is left alone: a lookup proves the access token, not the
        refresh token that ``reauth_required`` is about.
        """
        now = _now()

        def change(row: Account) -> Account:
            return replace(
                row,
                handle=identity.handle.lower(),
                provider_user_id=identity.provider_user_id,
                binding_id=binding_id,
                verified_at=now,
            )

        self._update(canonical_alias(alias), change)

    def mark_status(self, alias: str, status: str) -> Account | None:
        if status not in STATUSES:
            raise PulsarError(INVALID_ARGUMENT, f"unknown account status {status!r}")
        return self._update(canonical_alias(alias), lambda row: replace(row, status=status))

    def logout(self, alias: str) -> Account:
        """Delete ``alias``'s tokens under its refresh lock; the row stays, ``revoked``."""
        alias = canonical_alias(alias)
        if alias not in self._read():
            raise self._unknown(alias, self._read(), from_default=False)
        self.store(alias).clear(on_cleared=lambda: self.mark_status(alias, REVOKED))
        account = self._read().get(alias)
        assert account is not None
        return account

    # -- migration from the phase 1 single-account layout ---------------------

    def _orphans(self, rows: dict[str, Account]) -> list[str]:
        """Aliases whose directory holds credentials but that have no row."""
        try:
            entries = sorted(self.paths.accounts_dir.iterdir())
        except FileNotFoundError:
            return []
        out: list[str] = []
        for entry in entries:
            alias = alias_from_slug(entry.name)
            if alias and alias not in rows and (entry / "tokens.enc").exists():
                out.append(alias)
        return out

    def _legacy_identity(self, bundle: TokenBundle | None) -> Identity | None:
        """``whoami.json``'s identity if it describes ``bundle``'s binding."""
        if bundle is None:
            return None
        try:
            cached = as_object(json.loads(self.paths.whoami_cache.read_text()))
        except (FileNotFoundError, ValueError):
            return None
        if cached is None or cached.get("binding_id") != bundle.binding_id:
            return None
        user_id, username = cached.get("user_id"), cached.get("username")
        if not isinstance(username, str) or not username or user_id is None:
            return None
        return Identity(provider_user_id=str(user_id), handle=username.lower())

    def _migration_pending(self, rows: dict[str, Account], target: str | None) -> bool:
        legacy = self.paths.token_file.exists()
        return (
            (legacy and (target is not None or not rows))
            or bool(self._orphans(rows))
            or (not legacy and bool(rows) and self.paths.whoami_cache.exists())
        )

    def migrate_legacy(self, settings: Settings, alias: str | None = None) -> MigrationResult:
        """Move the phase 1 root ``tokens.enc`` into the account layout.

        Without ``alias`` (automatic, on first use) it runs only while the
        registry is empty, and names the account ``settings.default_account``,
        else ``x:<username>`` from a ``whoami.json`` that describes the stored
        binding; otherwise the bundle stays where it is (``needs_alias``).
        ``alias`` (``pulsar auth migrate``) names it explicitly. A cached
        identity that contradicts the alias is ``account_mismatch``, and a
        target that already has credentials is refused; neither moves anything.

        Crash-safe and idempotent: under the legacy refresh lock (so a phase 1
        process cannot rotate the bundle mid-move) and the target's refresh
        lock, the bundle is renamed into the account directory, then the row is
        written, then ``whoami.json`` removed. A re-run after a crash at any
        step finishes the job: an account directory with credentials and no
        row is recorded, and a leftover ``whoami.json`` is removed.
        """
        target = canonical_alias(alias) if alias is not None else None
        rows = self._read()
        if not self._migration_pending(rows, target):
            if self.paths.token_file.exists():
                return MigrationResult(
                    "ignored",
                    message=(
                        f"legacy credentials at {self.paths.token_file} were not migrated "
                        "because accounts are already registered; run "
                        "`pulsar auth migrate --account x:<handle>` to adopt them"
                    ),
                )
            return MigrationResult("none")
        legacy = FernetFileStore(self.paths)
        with legacy.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS):
            return self._migrate_locked(settings, target, legacy)

    def _migrate_locked(
        self, settings: Settings, target: str | None, legacy: FernetFileStore
    ) -> MigrationResult:
        with self._lock():
            rows = self._read()
            adopted = self._adopt_orphans(rows)
        result = MigrationResult("none", adopted=adopted)
        if legacy.token_file.exists():
            if rows and target is None:
                result = MigrationResult("ignored", adopted=adopted)
            else:
                result = self._move_legacy(settings, target, legacy, adopted)
        if not legacy.token_file.exists() and self._read():
            # Last step, so a crash before it leaves the identity for recovery.
            self._drop_legacy_identity()
        return result

    def _drop_legacy_identity(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.paths.whoami_cache.unlink()

    def _adopt_orphans(self, rows: dict[str, Account]) -> tuple[str, ...]:
        """Record account directories that hold credentials but have no row (caller locks)."""
        orphans = self._orphans(rows)
        for alias in orphans:
            bundle = self.store(alias).load()
            identity = (
                self._legacy_identity(bundle) if alias_provider(alias) == LEGACY_PROVIDER else None
            )
            if identity is not None and identity.handle != alias.partition(":")[2]:
                identity = None
            rows[alias] = Account(
                alias=alias,
                provider=alias_provider(alias),
                handle=identity.handle if identity else None,
                provider_user_id=identity.provider_user_id if identity else None,
                scopes=tuple(bundle.scope.split()) if bundle else (),
                binding_id=bundle.binding_id if bundle else None,
            )
        if orphans:
            self._write(rows)
        return tuple(orphans)

    def _move_legacy(
        self,
        settings: Settings,
        target: str | None,
        legacy: FernetFileStore,
        adopted: tuple[str, ...],
    ) -> MigrationResult:
        bundle = legacy.load()
        identity = self._legacy_identity(bundle)
        alias = target or settings.default_account
        if alias is None and identity is not None:
            alias = canonical_alias(f"{LEGACY_PROVIDER}:{identity.handle}")
        if alias is None:
            return MigrationResult("needs_alias", message=LEGACY_NEEDS_ALIAS, adopted=adopted)
        if alias_provider(alias) != LEGACY_PROVIDER:
            raise PulsarError(
                INVALID_ARGUMENT,
                f"the legacy credentials are X credentials; {alias} is not an x: account",
                detail={"account": alias},
            )
        if identity is not None:
            check_handle(alias, identity.handle, settings)
        store = self.store(alias)
        with store.refresh_lock_sync(REFRESH_LOCK_WAIT_SECONDS):
            if store.token_file.exists():
                raise PulsarError(
                    INVALID_ARGUMENT,
                    f"{alias} already has credentials; not overwriting them with the legacy "
                    f"bundle. Run `pulsar auth logout --account {alias}` first if the legacy "
                    "one should win",
                    detail={"account": alias},
                )
            os.replace(legacy.token_file, store.token_file)
            _fsync_dir(store.token_file.parent)
            _fsync_dir(self.paths.home)
            with self._lock():
                rows = self._read()
                rows[alias] = Account(
                    alias=alias,
                    provider=alias_provider(alias),
                    handle=identity.handle if identity else None,
                    provider_user_id=identity.provider_user_id if identity else None,
                    scopes=tuple(bundle.scope.split()) if bundle else (),
                    binding_id=bundle.binding_id if bundle else None,
                )
                self._write(rows)
        return MigrationResult("migrated", alias=alias, adopted=adopted)
