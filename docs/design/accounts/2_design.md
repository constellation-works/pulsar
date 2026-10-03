---
title: Accounts — Design
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Accepted
feature: accounts
doc_role: design
type: design
summary: Login with identity check, encrypted per-account bundles, locked refresh, migration, offline auth-health alarms and the storage boundary.
tags: [accounts, auth, oauth, credentials, storage]
paths: ["src/pulsar/app/core/account/registry.py", "src/pulsar/app/core/account/store.py", "src/pulsar/app/core/account/clients.py", "src/pulsar/app/core/channels/credentials.py", "src/pulsar/app/login.py", "src/pulsar/internal/fs/paths.py", "src/pulsar/internal/fs/files.py", "src/pulsar/app/core/channels/x/auth.py", "src/pulsar/app/core/channels/x/client.py", "src/pulsar/cli/commands/auth.py"]
related_features: [publishing, surfaces]
related_artifacts: [ORB-13008, ORB-13009, ORB-13027, ORB-13028, ORB-13039, ORB-13138, ORB-13279, ORB-13725]
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

`<slug>` is `provider--handle` ([paths.py](../../../src/pulsar/internal/fs/paths.py)
`account_slug`): provider `[a-z0-9]`, handle `[a-z0-9._-]` without a leading dot, at most 100
characters. The mapping is injective and reversible, and no alias can name a path outside
`accounts/`. Aliases are case-insensitive and tolerate a leading `@` (`X:@ConstWorks` is
`x:constworks`); anything else is `invalid_argument`.

The home is `PULSAR_HOME`, else `~/.config/pulsar`. Core never reads the environment or `$HOME`
itself: a surface resolves both once (`Paths.from_environ(environ, user_home)`) and passes the
`Paths` down; `Paths.user_home` is also what `~` in `config.toml` expands against. The home
itself must not be a symlink ([decision](./4_decisions.md#refuse-a-symlinked-home-resolve-nothing)).

## 2. Login

`pulsar auth login --account x:<handle> [--client-id ID] [--no-browser]` is human-only and runs
outside every tool surface. The OAuth steps are X's
([app/core/channels/x/auth.py](../../../src/pulsar/app/core/channels/x/auth.py)) and store nothing;
the identity check and the binding are the app's ([app/login.py](../../../src/pulsar/app/login.py)).

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
`client.json` (it is not a secret), rewritten under `accounts.lock`
([account/clients.py](../../../src/pulsar/app/core/account/clients.py)).

The consent URL is shown through `login(notify=...)`, stderr by default, never stdout. The
loopback listener is not trusted for being loopback: it answers only a `Host` of exactly
`127.0.0.1:<port>` or `localhost:<port>` (else 400 for none, 421 for another), refuses any
`Origin` but that same `http://` authority (403), and records a redirect only when its `state`
is this login's. A forged or stray request is refused and the wait goes on; only the real
redirect (a `code`, or X's `error`) ends it.

## 3. Selecting and Checking an Account

A call that names no `account` acts as `default_account`, else as the only bound (not revoked)
account. With several bound and no default it is `invalid_argument`; an unregistered alias is
`unknown_account` with `detail.known`.

Before every write the bound handle must equal the alias's handle and, when set,
`expected_handle` ([account/registry.py](../../../src/pulsar/app/core/account/registry.py) `check_handle`,
`require_expected`); otherwise `account_mismatch` with `detail: {alias, expected_handle,
bound_handle}` and nothing is sent. The ledger row records the account's alias, user id and
handle.

The write is sent with a token of the binding that check was for. The identity comes with the
stored bundle's `binding_id`, and the write's client is pinned to it
([x/client.py](../../../src/pulsar/app/core/channels/x/client.py) `XClient.pinned`): every
request (uploads, the POST, a 401 retry) re-reads the stored bundle and refuses one of another
binding with `account_mismatch` (retryable) before anything is sent (a logout is
`auth_expired`), so a re-login or logout between the check and the POST leaves the ledger row
`failed`, not a post under an unchecked identity. A refresh carries the binding forward, so a
token refreshed in between, by this process or another, still posts. A `/users/me` lookup is
pinned the same way, so a re-login during it fails the lookup instead of recording the new
login's identity for the old binding.

## 4. Status

`pulsar auth status` ([health.py](../../../src/pulsar/app/health.py)) reports each
account with every key present (null when unknown): `alias`, `status`, `expected_handle`,
`mismatch`, `token_state`, `account`, `account_source`, `verified`, `refreshed`,
`reauth_required`, `error`, and `health` with its `reason`. Exit 0 only when every reported
account is `healthy`.

- **`health`** is three-valued, because unknown is never reported as healthy:
  `healthy` (active, identity cached for this binding and matching, token valid or just
  refreshed); `unverified` (nothing known to be wrong, but local state cannot settle it: no
  cached identity, or an expired access token whose refresh was not exercised); `unhealthy`
  (not bound, logged out, re-authorization required, a mismatch, unreadable storage).
- **Default.** Local state only: no network call and no write. Legacy credentials are
  reported under `legacy`, not migrated. The Orbit `pulsar.status` tool uses this.
- **`--live`.** The proof: rotates the token pair through the account's refresh lock, fetches
  the identity from X and rewrites the registry row. One `/users/me` read per account.
- **`--offline`** is a deprecated no-op (the default is offline) and warns on stderr.

Cached identity is tagged with the `binding_id` it was fetched under, so after a re-login it
is ignored until `/users/me` has been asked again, even if a lookup started before the
re-login finishes after it.

## 5. Refresh

Refresh is automatic ([app/core/channels/x/client.py](../../../src/pulsar/app/core/channels/x/client.py)).
Each account's refresh runs under an exclusive `flock` on `accounts/<slug>/refresh.lock`: the
first process refreshes, the others wait (up to 45 s, then `lock_timeout`) and reuse the bundle
it saved. Accounts do not wait for each other. Store reads and writes run in a worker thread,
so a refresh never blocks the event loop; one refresh per account is in flight per process.

Every lock wait is bounded ([fsutil.py](../../../src/pulsar/internal/fs/files.py) `hold_lock`): the
refresh locks at 45 s, `accounts.lock` at 15 s. Right after acquiring, the holder writes
`{pid, label, acquired_at}` into the lock file; a waiter that times out reports it in the
message and `detail.holder` (null, "an unknown holder", when there is no record, e.g. an older
pulsar holds it). The record is diagnostic only and may name the previous holder for a moment;
ownership is the `flock`. Lock order everywhere: the legacy root refresh lock, an account's
refresh lock, then `accounts.lock`.

If X still rejects a refresh token because a process that ignores the lock rotated it first,
pulsar re-reads the store and uses the newer bundle instead of reporting `auth_expired`. A 401
after a sibling's rotation retries once with the stored bundle without refreshing again.

When a refresh fails for real (revoked, app reset), tools return `auth_expired` and the
registry row becomes `reauth_required`; the message names the account and the home, as the
command a human runs: `PULSAR_HOME=<home> pulsar auth login --account <alias>`. `auth logout`
deletes the bundle under the same lock and marks the row `revoked`; the row stays for history.

A token response without `expires_in` is stored as already expiring, so the next call
refreshes rather than trusting an invented lifetime. A failure inside pulsar after X answered
(the save failed) is `internal`, never `outcome_unknown`: only the token POST was sent.

## 6. Storage at Rest

[store.py](../../../src/pulsar/app/core/account/store.py) `FernetFileStore` implements the
`CredentialStore` interface ([channels/credentials.py](../../../src/pulsar/app/core/channels/credentials.py));
the rest of pulsar codes against the interface.

- Fernet encryption with one `key` at the home root for all accounts.
- Saves are atomic (temp file, `fsync`, rename, directory `fsync`), so a crash mid-refresh never
  destroys the only refresh token. The `key` is created once with `publish_new_private` (temp
  file, `fsync`, `link`, directory `fsync`), so concurrent first saves agree on one key, and a
  new directory's parent is fsynced too.
- Every file pulsar creates is 0600 and every directory 0700, regardless of umask
  ([fsutil.py](../../../src/pulsar/internal/fs/files.py)).
- pulsar refuses to load or save credentials when the home or an account directory is wider
  than 0700, or `key` / `tokens.enc` / `accounts.json` wider than 0600 or owned by another uid:
  `insecure_storage` with the exact `chmod` in `detail.fix`. It never narrows a wide home
  itself. A corrupt `key` is also `insecure_storage`.
- None of that state may be reached through a symlink: the checks use `lstat`, a symlinked
  file, account directory or home is `insecure_storage` naming its target, and writes and lock
  files are opened `O_NOFOLLOW`. `config.toml` and `client.json` are not secret but steer pulsar
  (budgets, media roots, `expected_handle`; which OAuth app is authorized): they may be
  world-readable but not a symlink, another user's, or group/world-writable (`chmod go-w`).
- A bundle that is there but cannot be read is `credentials_unreadable`, never "not logged in":
  a replaced `key` or corrupt ciphertext (restore the key; only if it is lost, remove the
  bundle and log in again), or a bundle a newer pulsar wrote with fields this one does not
  know (upgrade). pulsar never overwrites it on its own.
- `accounts.json` carries a `version` and `min_reader_version`, the oldest version that still
  reads it correctly (a version that only adds fields keeps it; one that removes, renames or
  reinterprets a field raises it). One from a newer pulsar is read to resolve an account only
  when it names this version a reader; otherwise it is refused with `invalid_config`.
  Every write to a newer registry (login, logout, status changes, migration)
  is refused with `invalid_config` naming the file and both versions, before anything is
  stored.

## 7. Legacy Migration

A home from before accounts has `tokens.enc` and `whoami.json` at its root.
`migrate_legacy` moves the bundle to `accounts/x--<handle>/` as `default_account` if
configured, else as `x:<username>` from a `whoami.json` that describes the stored login. It
runs as `pulsar migrate --confirm`, as `pulsar auth migrate --confirm` (which infers the alias
the same way when `--account` is not given), and on the first command or tool call that
writes; reports never migrate (they show `legacy` from `legacy_status`), and `auth migrate`
without `--confirm` is one. If neither names it (`needs_alias`), every call says to run
`pulsar auth migrate --account x:<handle> --confirm`. Without `--account` the bundle is
only moved while no account is registered (`ignored` otherwise); `--account` adopts it beside
registered ones. A cached identity that contradicts the alias is `account_mismatch` and moves
nothing, and migration never overwrites an account that already has credentials.

The move holds the old root refresh lock, renames the bundle, writes the registry row, then
removes `whoami.json`; re-running after a crash at any step finishes the job.

`legacy_status` answers what `migrate_legacy` would do (`none`, `pending` with the target
alias, `needs_alias`, `ignored`) without taking a lock or changing anything, for read-only
commands.

## 8. Offline Auth-health Alarm

The plugin ships [auth-health.yaml](../../../.orbit-plugin/definitions/auto_tasks/auth-health.yaml)
([ORB-13725]), seeded as `pulsar-auth-health`, disabled until a human enables it.
Its daily schedule is `0 8 * * *` in Orbit's scheduler timezone. It requires exactly
`pulsar.status`: one offline read of every account, with no refresh, live probe, login,
publish or approval. The template carries `no-diff-expected`; it writes no files.
Its evidence lives in the execution summary and follow-up task state.

Healthy accounts raise nothing. For each unverified or unhealthy account the executor
uses the publishing skill's [offline planner](../../../.orbit-plugin/skills/publish/scripts/auth_health.py)
on the status output and a complete Orbit task-list envelope. The helper returns
arguments for proposed human attention tasks, copying `alias`, `health`, `token_state`,
`reason`, `reauth_required` and the account's exact returned `attention` text. An
**unverified** account gets a medium-priority **Verify** task: the human runs the returned
`PULSAR_HOME=<home> pulsar auth status --live --account <alias>` to exercise refresh and
prove identity. **Re-authorization required** gets a high-priority **Re-authorize** task
with the returned `PULSAR_HOME=<home> pulsar auth login --account <alias>`. Other unhealthy
accounts get a **Repair** task with their returned attention; no remedy is invented.
Only a human at a terminal on the posting host performs these commands.

The scheduled check uses `dedupe: skip_if_open`. Follow-ups use the stable account tag
`pulsar-auth-health:<alias>` and the listing tag `pulsar-auth-attention`. All non-terminal
tasks count, including proposed, blocked and someday: daily reruns reuse the task ID
instead of filing another. A verification task that now requires re-authorization is
escalated by updating its title and priority and adding the current evidence as a
comment; its existing description and status remain. Terminal tasks allow a new alarm.
A truncated task list fails planning, so the executor must obtain a complete list
before creating anything. Overall status attention about unresolved writes or legacy
migration alone does not create an account alarm when account health is healthy.

## 9. Concerns & Honest Limitations

- **The alarm needs opt-in and an executor.** It ships disabled, observes only local state,
  and leaves live verification and re-authorization to a human. Its planner deterministically
  selects task arguments; the agent performs the Orbit task-tool calls. Open-task dedupe
  uses a workspace-local snapshot, not an atomic create constraint; the scheduled check's
  `skip_if_open` prevents overlapping scheduled checks, but simultaneous manual runs can race.

- **Same-uid processes can decrypt.** The key sits beside the ciphertext. Encryption protects
  against the bundle leaking through backups, `grep` over the home, a stray commit or another
  local user; it does not protect against any process running as the same uid, including an
  agent sandbox that can read the home. Until the home is in Orbit plugin state carved out by
  [ORB-13008], the boundary is: the agent never holds secrets, the connector process does.
- **Bundles from before `binding_id` cannot be told apart.** A bundle saved before bindings
  existed has none, so a swap between two such bundles passes the pin. Every login since mints
  one, so a re-login is always seen.
- **Login needs a human with a browser and a loopback port.** Re-authorization on a headless
  host goes through SSH port forwarding; there is no device-code fallback.
- **Refresh lock wait is bounded but blocking.** A process holding the lock for 45 s (a hung
  token endpoint) turns every other caller's write into `lock_timeout`, which names it.
- **Only the home's own path is checked for symlinks.** Directories above the home (say a
  symlinked `~/.config`) are the operator's choice of `PULSAR_HOME` and are not inspected.

## Task References

- [ORB-13008] — Orbit: carve plugin state out of other plugins' and agents' reach (ws_orbit).
- [ORB-13009] — Orbit: host-held secrets with compare-and-swap rotation (ws_orbit).
- [ORB-13027] — added atomic owner-only storage, the refresh lock and `auth status --live`.
- [ORB-13028] — added the registry, aliases, verified login, `expected_handle` and migration.
- [ORB-13039] — the phase-1 review that found the unpinned post token.
- [ORB-13138] — aligned storage, locks, errors and the login listener with the constellation standards.
- [ORB-13279] — pinned a write's token to the binding whose identity was checked.
- [ORB-13725] — added the disabled daily offline auth-health alarm and per-account human follow-ups.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
