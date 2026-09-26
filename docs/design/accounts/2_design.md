---
title: Accounts — Design
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: accounts
doc_role: design
type: design
summary: Login with identity check, the registry, encrypted per-account bundles, locked refresh, legacy migration and the storage boundary.
tags: [accounts, auth, oauth, credentials, storage]
paths: ["src/pulsar/core/accounts.py", "src/pulsar/core/store.py", "src/pulsar/core/paths.py", "src/pulsar/core/fsutil.py", "src/pulsar/providers/x/auth.py", "src/pulsar/providers/x/client.py", "src/pulsar/surfaces/cli.py"]
related_features: [publishing, surfaces]
related_artifacts: [ORB-13008, ORB-13009, ORB-13027, ORB-13028, ORB-13039]
---

# Accounts — Design

How accounts are bound, stored, refreshed and checked today. Where the tokens should live
long term (host-held secrets) is in [3_vision.md](./3_vision.md).

## 1. Home Layout

```
key                          one Fernet key for every account (0600)
client.json                  OAuth client ids, one per provider
accounts.json                the registry
accounts.lock                serialises registry read-modify-writes
accounts/<slug>/tokens.enc   that account's encrypted bundle
accounts/<slug>/refresh.lock that account's cross-process refresh lock
config.toml                  operator settings (optional)
ledger.sqlite3, writes.jsonl the write ledger and its export (publishing)
```

`<slug>` is `provider--handle` ([paths.py](../../../src/pulsar/core/paths.py)
`account_slug`): provider `[a-z0-9]`, handle `[a-z0-9._-]` without a leading dot, at most 100
characters. The mapping is injective and reversible, and no alias can name a path outside
`accounts/`. Aliases are case-insensitive and tolerate a leading `@` (`X:@ConstWorks` is
`x:constworks`); anything else is `invalid_argument`.

## 2. Login

`pulsar auth login --account x:<handle> [--client-id ID] [--no-browser]` is human-only and runs
outside every tool surface ([providers/x/auth.py](../../../src/pulsar/providers/x/auth.py)).

1. OAuth 2.0 authorization code with PKCE, public client, scopes `tweet.read tweet.write
   users.read offline.access`, loopback callback `http://127.0.0.1:8976/callback`. On a remote
   host the callback is forwarded with `ssh -L 8976:127.0.0.1:8976`.
2. Before anything is stored, pulsar asks X (`GET /2/users/me`) whom the new token belongs to.
   If the handle is not the alias's, or not the configured `expected_handle`, the login is
   refused with `account_mismatch` naming both handles and nothing is written.
3. The bundle is saved under the account's refresh lock, a fresh `binding_id` is minted, and
   the registry row is written before the lock is released, so a refresh already in flight
   cannot write the previous login's rotated tokens over the new one.

`--account` defaults to `default_account`. The client id is remembered per provider in
`client.json` (it is not a secret).

## 3. Selecting and Checking an Account

A call that names no `account` acts as `default_account`, else as the only bound (not revoked)
account. With several bound and no default it is `invalid_argument`; an unregistered alias is
`unknown_account` with `detail.known`.

Before every write the bound handle must equal the alias's handle and, when set,
`expected_handle` ([accounts.py](../../../src/pulsar/core/accounts.py) `check_handle`,
`require_expected`); otherwise `account_mismatch` with `detail: {alias, expected_handle,
bound_handle}` and nothing is sent. The ledger row records the account's alias, user id and
handle.

## 4. Status

`pulsar auth status` reports each account: `alias`, `status`, `expected_handle`, `mismatch`,
`token_state`, `account`, `account_source`, `verified`, `reauth_required`, `healthy`, and a
`note` when unproven; exit 0 only when every reported account is healthy.

- **Default (cached).** Reads identity from the registry, so it names the account even when
  the refresh token is dead, and says so (`verified: false`, a `note`).
- **`--live`.** The proof: rotates the token pair through the account's refresh lock, fetches
  the identity from X and rewrites the registry row. One `/users/me` read per account.
- **`--offline`.** Stored state only; never calls X. The Orbit `pulsar.status` tool uses this.

Cached identity is tagged with the `binding_id` it was fetched under, so after a re-login it
is ignored until `/users/me` has been asked again, even if a lookup started before the
re-login finishes after it.

## 5. Refresh

Refresh is automatic ([providers/x/client.py](../../../src/pulsar/providers/x/client.py)).
Each account's refresh runs under an exclusive `flock` on `accounts/<slug>/refresh.lock`: the
first process refreshes, the others wait (up to 45 s, then `api_error`) and reuse the bundle it
saved. Accounts do not wait for each other.

If X still rejects a refresh token because a process that ignores the lock rotated it first,
pulsar re-reads the store and uses the newer bundle instead of reporting `auth_expired`. A 401
after a sibling's rotation retries once with the stored bundle without refreshing again.

When a refresh fails for real (revoked, app reset), tools return `auth_expired` and the
registry row becomes `reauth_required`; a human re-runs `auth login`. `auth logout` deletes the
bundle under the same lock and marks the row `revoked`; the row stays for history.

## 6. Storage at Rest

[store.py](../../../src/pulsar/core/store.py) `FernetFileStore` implements the
`CredentialStore` interface; the rest of pulsar codes against the interface.

- Fernet encryption with one `key` at the home root for all accounts.
- Saves are atomic (temp file, `fsync`, rename, directory `fsync`), so a crash mid-refresh never
  destroys the only refresh token, and concurrent first saves agree on one key.
- Every file pulsar creates is 0600 and every directory 0700, regardless of umask
  ([fsutil.py](../../../src/pulsar/core/fsutil.py)).
- pulsar refuses to load or save credentials when the home or an account directory is wider
  than 0700, or `key` / `tokens.enc` / `accounts.json` wider than 0600 or owned by another uid:
  `insecure_storage` with the exact `chmod` in `detail.fix`. It never narrows a wide home
  itself. A corrupt `key` is also `insecure_storage`.

## 7. Legacy Migration

A home from before accounts has `tokens.enc` and `whoami.json` at its root. The first command
or tool call moves the bundle to `accounts/x--<handle>/` as `default_account` if configured,
else as `x:<username>` from a `whoami.json` that describes the stored login. If neither names
it, every call says to run `pulsar auth migrate --account x:<handle>`. A cached identity that
contradicts the alias is `account_mismatch` and moves nothing, and migration never overwrites
an account that already has credentials.

The move holds the old root refresh lock, renames the bundle, writes the registry row, then
removes `whoami.json`; re-running after a crash at any step finishes the job.

## 8. Concerns & Honest Limitations

- **Same-uid processes can decrypt.** The key sits beside the ciphertext. Encryption protects
  against the bundle leaking through backups, `grep` over the home, a stray commit or another
  local user; it does not protect against any process running as the same uid, including an
  agent sandbox that can read the home. Until the home is in Orbit plugin state carved out by
  [ORB-13008], the boundary is: the agent never holds secrets, the connector process does.
- **The post token is not yet bound to the looked-up identity.** The handle check reads the
  registry's identity for the current binding; [ORB-13039] proposes binding the token used for
  a post to the identity fetched with it, closing the window where a re-login lands between
  the check and the send.
- **Login needs a human with a browser and a loopback port.** Re-authorization on a headless
  host goes through SSH port forwarding; there is no device-code fallback.
- **Refresh lock wait is bounded but blocking.** A process holding the lock for 45 s (a hung
  token endpoint) turns every other caller's write into `api_error`.

## Task References

- [ORB-13008] — Orbit: carve plugin state out of other plugins' and agents' reach (ws_orbit).
- [ORB-13009] — Orbit: host-held secrets with compare-and-swap rotation (ws_orbit).
- [ORB-13027] — added atomic owner-only storage, the refresh lock and `auth status --live`.
- [ORB-13028] — added the registry, aliases, verified login, `expected_handle` and migration.
- [ORB-13039] — proposed: bind the post token to the looked-up identity.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
