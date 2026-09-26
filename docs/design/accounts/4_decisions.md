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
paths: ["src/pulsar/providers/x/auth.py", "src/pulsar/core/fsutil.py", "src/pulsar/providers/x/client.py", "src/pulsar/core/store.py"]
related_features: [publishing]
related_artifacts: [ORB-13027, ORB-13028]
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
**Code anchors:** `src/pulsar/providers/x/auth.py::complete_login`

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
**Code anchors:** `src/pulsar/core/fsutil.py::require_private`

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

## Re-read the store when a refresh is rejected

**Recorded:** 2026-09-26 · [ORB-13027]
**Code anchors:** `src/pulsar/providers/x/client.py::XClient`

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

## Task References

- [ORB-12124] — built the first connector with no credential parameters.
- [ORB-13027] — hardened storage and refresh.
- [ORB-13028] — verified logins and `expected_handle`.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
