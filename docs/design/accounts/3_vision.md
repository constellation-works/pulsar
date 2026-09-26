---
title: Accounts — Vision
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Draft
feature: accounts
doc_role: vision
type: design
summary: Where credentials should live (host-held secrets), per-provider auth flows, and the open questions about re-authorization.
tags: [accounts, auth, credentials, secrets]
paths: ["src/pulsar/core/store.py", "src/pulsar/core/adapter.py"]
related_features: [channels, surfaces]
related_artifacts: [ORB-13008, ORB-13009, ORB-13030, ORB-13031]
---

# Accounts — Vision

Where account storage and auth are heading. Nothing here is built unless
[2_design.md](./2_design.md) says so.

## 1. Open Questions

1. **When does the store become Orbit's?** [ORB-13009] proposes host-held secrets with
   compare-and-swap rotation (`spec.secrets`). If it lands, `FernetFileStore` is replaced by a
   `CredentialStore` over it and the key-beside-ciphertext limitation goes away. Until then the
   home moves into `{{plugin_state}}` once the host's Orbit carries [ORB-13008].
2. **How is re-authorization noticed?** Today `auth_expired` surfaces only when a write fails.
   Phase 4 ([ORB-13030]) plans a daily auth-health auto-task that files a task when an account
   is `reauth_required`; what it may call (`--offline` only, or a paid `--live` probe) is open.
3. **Is a device-code or remote-callback login worth it?** SSH forwarding works for one
   operator; it does not scale to several people binding accounts.
4. **Should the registry record who bound an account?** Today it records when and which
   binding, not which human.

## 2. Prior Work

### Credential brokers

Connector processes that hold OAuth tokens on behalf of agents (MCP servers with their own auth,
secret managers with lease-based access). pulsar follows the same split: intent-level tools,
credentials behind the process boundary.

### Rotating refresh tokens

OAuth 2.0 Security BCP recommends refresh-token rotation for public clients. Rotation makes
concurrent refresh a correctness problem, which is why pulsar serialises it per account and
re-reads the store on rejection.

## 3. What May Be Distinctive

Verifying the token's owner at login and re-checking the bound handle before every write turns
"post as the right account" from a runbook step into an invariant. Most connectors trust
whatever token they were given.

## 4. References

**pulsar-internal**

- [Accounts — Design](./2_design.md)
- [Channels — Vision](../channels/3_vision.md) for per-provider auth flows

**External**

- OAuth 2.0 for Native Apps (RFC 8252); PKCE (RFC 7636); OAuth 2.0 Security Best Current
  Practice (RFC 9700).

## Task References

- [ORB-13008] — Orbit: private plugin state directories (ws_orbit).
- [ORB-13009] — Orbit: host-held secrets with compare-and-swap rotation (ws_orbit).
- [ORB-13030] — phase 4: plans the auth-health auto-task.
- [ORB-13031] — phase 5: the second provider, whose auth flow differs.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
