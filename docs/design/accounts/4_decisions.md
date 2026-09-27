---
title: Accounts — Decisions
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: accounts
doc_role: decisions
type: design
summary: Why logins are verified before storing, why a wide home is refused rather than fixed, and why refresh re-reads the store.
tags: [accounts, auth, credentials]
paths: ["src/pulsar/app/core/channels/x/auth.py", "src/pulsar/internal/fs/files.py", "src/pulsar/app/core/channels/x/client.py", "src/pulsar/app/core/account/store.py", "src/pulsar/internal/fs/paths.py"]
related_features: [publishing]
related_artifacts: [ORB-13027, ORB-13028, ORB-13138]
---

# Accounts — Decisions

Non-obvious decisions about accounts and credentials. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## Credentials never cross a tool boundary

**Recorded:** 2026-09-11 · [ORB-12124]

### Context

Agents call pulsar through MCP, the CLI and the Orbit plugin. Anything a tool accepts or
returns can end up in a transcript, a log, a task comment or another agent's context.

### Decision

No tool on any surface accepts or returns a token, key or secret, and there is no flag that
allows it. Authorization is a human action on the host (`pulsar auth login`), never a tool.
New tools and schemas are checked for credential-shaped parameters in tests
(`tests/test_server.py`, `tests/test_orbit_tool.py`).

### Consequences

- An agent can be handed pulsar without being trusted with the account.
- Cost: an expired or revoked account cannot be repaired by an agent; a human with a browser
  must re-run the login, and unattended routines stop until they do.

## Verify the token's owner before storing a login

**Recorded:** 2026-09-26 · [ORB-13028]
**Code anchors:** `src/pulsar/app/core/channels/x/auth.py::complete_login`

### Context

On 2026-09-16 posts went to the wrong X account because a login approved in a browser signed
in as another account had been stored. The runbook's "make sure it is @constworks" was a
manual step and was skipped.

### Decision

A login asks X whom the new token belongs to before anything is written. A handle that is not
the alias's, or not `expected_handle`, refuses the login with `account_mismatch`; every write
re-checks the bound handle the same way.

### Consequences

- An alias always posts as the handle it is named for.
- Cost: login needs one extra `/users/me` call and fails when that read fails, even if the
  token itself is fine.

## Refuse a wide home, never narrow it

**Recorded:** 2026-09-26 · [ORB-13027]
**Code anchors:** `src/pulsar/internal/fs/files.py::require_private`

### Context

The box's home arrived with 775/664 modes after a copy. Silently `chmod`ing it would make
pulsar usable again but hide that the token was readable by others for some time.

### Decision

When storage is wider than owner-only, pulsar fails closed with `insecure_storage` and the
exact `chmod` to run. It never narrows permissions itself. The rule extends to any new file
pulsar keeps secrets in.

### Consequences

- The operator decides whether an exposed token must be rotated.
- Cost: a harmless umask accident stops every write until a human runs one command.

## Refuse a symlinked home, resolve nothing

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/internal/fs/paths.py::Paths.check_home`, `src/pulsar/internal/fs/files.py::require_private`

### Context

State holding secrets should not be reached through a symlink. The operator
chooses the home (`PULSAR_HOME`), so a symlinked home could be a deliberate relocation, or a
link planted to point pulsar's key and tokens somewhere another process controls. Resolving it
would make the check pass on whatever the link points at.

### Decision

The strict reading: pulsar resolves nothing. A home that is a symlink is `insecure_storage`,
and the fix names the real directory to set `PULSAR_HOME` to. The same holds for every file
and directory below it (`key`, `tokens.enc`, `accounts.json`, `client.json`, `config.toml`,
the ledger, the account directories), checked with `lstat` and written `O_NOFOLLOW`.
Directories above the home are not inspected: naming the home is the operator's act.

### Consequences

- A link swapped in for the home or a credential file stops pulsar instead of redirecting it.
- Cost: an operator who keeps state behind a symlink (a dotfiles checkout, a moved disk) must
  point `PULSAR_HOME` at the real path, and a symlinked `config.toml` has to become a file.

## Unreadable credentials are not a logout

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/app/core/account/store.py::FernetFileStore.load`

### Context

`load` used to return "no bundle" when decryption failed. Every caller then reported
`auth_expired` and sent a human to `pulsar auth login`, which overwrites the bundle: the fix for
a replaced key destroyed the one credential that could still have been recovered. A bundle with
fields from a newer pulsar was treated the same way.

### Decision

A bundle that exists but cannot be decrypted, parsed, or understood is
`credentials_unreadable`, naming the file and the key. Nothing overwrites it on its own; the
message leads with restoring the key (or upgrading pulsar), and names logging in again only as
the last resort once the key is gone for good.

### Consequences

- A replaced key or a downgrade is recoverable instead of silently turned into a re-login.
- Cost: callers see one more code, and unattended routines stop until a human looks.

## Re-read the store when a refresh is rejected

**Recorded:** 2026-09-26 · [ORB-13027]
**Code anchors:** `src/pulsar/app/core/channels/x/client.py::XClient`

### Context

X rotates the refresh token on every use. The per-account `flock` serialises pulsar's own
processes, but an older pulsar mid-upgrade (or one on another host sharing a copied home)
ignores it and can rotate first.

### Decision

A refresh rejected with 400/401 re-reads the stored bundle; if another process saved a newer
one, use it instead of reporting `auth_expired`.

### Consequences

- A lost race costs one extra read, not a human re-login.
- Cost: a real revocation takes one more store read to be reported, and two hosts sharing a
  copied home can still kill each other's tokens: the lock is per host.

## The browser is opened, not supervised

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/app/core/channels/x/auth.py::authorize`

### Context

`pulsar auth login` opens the consent page with Python's `webbrowser`, which hands the URL to
the desktop's browser (often an already-running process) and returns. pulsar does not own
that process and cannot put it in a process group or bound it.

### Decision

Open the URL and wait only on pulsar's own loopback listener, with a deadline. `--no-browser`
prints the URL on stderr instead, for SSH sessions.

### Consequences

- Login uses the operator's real browser session, where they are signed in to X.
- Cost: pulsar cannot close the tab or clean up a browser it started.

## Task References

- [ORB-12124] — built the first connector with no credential parameters.
- [ORB-13027] — hardened storage and refresh.
- [ORB-13028] — verified logins and `expected_handle`.
- [ORB-13138] — strict symlink refusal and `credentials_unreadable` (standards alignment).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
