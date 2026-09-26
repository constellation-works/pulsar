---
title: Accounts — Overview
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: accounts
doc_role: overview
type: design
summary: How pulsar binds provider accounts to a host, stores their tokens, and proves which handle a write will post as.
tags: [accounts, auth, oauth, credentials]
paths: ["src/pulsar/core/accounts.py", "src/pulsar/core/store.py", "src/pulsar/core/paths.py", "src/pulsar/core/fsutil.py", "src/pulsar/providers/x/auth.py", "src/pulsar/providers/x/client.py"]
related_features: [publishing, surfaces]
related_artifacts: [ORB-12124, ORB-13027, ORB-13028, ORB-13039]
---

# Accounts — Overview

A pulsar home holds several provider accounts, each named by an alias (`x:constworks`) and
bound once by a human in a browser. The connector process owns the tokens: it stores them
encrypted, refreshes them, and checks before every write that they still belong to the handle
the alias names. No agent ever sees a token, and no tool can accept one.

## 1. Motivation

- **Agents must not hold credentials.** A Bearer token pasted into a chat ends up in
  transcripts, logs and prompts. pulsar keeps auth inside the connector and exposes only
  intent-level tools.
- **Posting as the wrong account happened.** On 2026-09-16 posts went to the wrong X account
  because the wrong token had been stored. A manual "make sure it is @constworks" step in the
  runbook did not prevent it; a check in code does.
- **X rotates the refresh token on every use.** Several pulsar processes share one home (a
  stdio server per client, the CLI, the Orbit backend), so an unserialised refresh by one of
  them kills the others' tokens.
- **More than one account, more than one provider.** Marketing and routines post as different
  accounts, and a second provider is planned; the account model had to stop being "the token".

## 2. Core Concepts

- **Home.** The directory that holds everything pulsar persists (`PULSAR_HOME`, default
  `~/.config/pulsar`; the Orbit plugin's is `$ORBIT_PLUGIN_STATE/home`).
- **Alias.** `provider:handle`, canonical lower case. It selects stored credentials; it never
  carries them.
- **Registry.** `accounts.json`: one row per alias with provider user id, handle, scopes,
  status (`active`, `reauth_required`, `revoked`), `bound_at`, `binding_id`, `verified_at`. No
  secrets.
- **Bundle.** One account's encrypted token pair, `accounts/<provider>--<handle>/tokens.enc`.
- **Binding.** One successful login, identified by `binding_id`; cached identity is only
  trusted for the binding it was fetched under.
- **Expected handle.** Config pin (`[accounts."x:constworks"] expected_handle`) that the bound
  handle must equal, else `account_mismatch`.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| Registry, alias canonicalisation, handle checks | [core/accounts.py](../../../src/pulsar/core/accounts.py) | [ORB-13028] |
| Encrypted bundle store, `CredentialStore` interface | [core/store.py](../../../src/pulsar/core/store.py) | [ORB-13027] |
| Home layout, alias → directory slug | [core/paths.py](../../../src/pulsar/core/paths.py) | [ORB-13028] |
| Owner-only files, atomic saves, `insecure_storage` | [core/fsutil.py](../../../src/pulsar/core/fsutil.py) | [ORB-13027] |
| OAuth 2.0 PKCE login and identity check | [providers/x/auth.py](../../../src/pulsar/providers/x/auth.py) | [ORB-12124], [ORB-13028] |
| Refresh under a per-account file lock | [providers/x/client.py](../../../src/pulsar/providers/x/client.py) | [ORB-13027] |
| `pulsar auth login | status | logout | migrate` | [surfaces/cli/commands/auth.py](../../../src/pulsar/surfaces/cli/commands/auth.py) | [ORB-13027], [ORB-13028] |

## Task References

- [ORB-12124] — built the first X connector with a single stored token.
- [ORB-13027] — hardened storage at rest, serialised refresh across processes, added `auth status --live`.
- [ORB-13028] — introduced the account registry, aliases, verified logins and `expected_handle`.
- [ORB-13039] — proposed: bind the post token to the looked-up identity, among other fixes.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
