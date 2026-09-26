---
type: design
summary: "Reference: error codes shared by every pulsar surface"
last_validated: 2026-09-26
---

# Reference: Error Codes

Every surface reports failures as `{code, message, retryable, detail?}` (MCP: `ok: false` at
the top level; Orbit: under `error`; CLI: the printed JSON, exit 1). `retryable` is true only
when repeating the identical call later can succeed. Codes are defined in
[core/errors.py](../../../../src/pulsar/core/errors.py).

| code | meaning | what to do |
|---|---|---|
| `auth_expired` | no account bound, logged out, no token, refresh failed (account then `reauth_required`), or legacy credentials needing `pulsar auth migrate` | stop; a human runs what `message` says |
| `account_mismatch` | the credentials belong to another handle than the alias's or `expected_handle` (`detail: {alias, expected_handle, bound_handle}`); nothing sent | stop; a human re-runs `pulsar auth login` as the right account |
| `unknown_account` | the alias is not registered; `detail.known` lists those that are | use a known alias, or have a human bind it |
| `insecure_storage` | the home or an account directory wider than 0700, or `key` / `tokens.enc` / `accounts.json` wider than 0600 or not owned by the user; or a corrupt `key` | stop; a human runs the `chmod` in `detail.fix` |
| `invalid_config` | `config.toml` has an unknown key or bad value, `accounts.json` is unreadable, the ledger is from a newer pulsar, or a `path` upload with no media roots | fix the file named in `message` |
| `invalid_text` | empty, over the length limit, control characters, reply and quote together | rewrite |
| `invalid_argument` | a malformed key, alias or id, no `account` while several are bound and none is default, a bad tool input | fix the argument |
| `invalid_plan` | a plan's shape is wrong (`detail.at` names where) | fix the plan |
| `invalid_media` | bad path or base64, outside the roots, not a regular file, a type that is not allowed or disagrees with its claim (`detail: {declared, sniffed}`), oversized, or failed video processing | fix the input |
| `secret_detected` | text, alt, media or key matches a credential pattern | rewrite; never retry verbatim |
| `unsupported` | the provider cannot do what the plan asks (thread, reply, quote) | change the plan |
| `not_due` | the plan's `not_before` is in the future (`detail.retry_after`) | publish after then |
| `quiet_hours` / `daily_cap` / `budget_exceeded` | the policy refused; nothing sent, no row (`detail.retry_after`) | wait; `retryable: false` means it can never pass on its own |
| `idempotency_conflict` | the key was used for a different request, tool or account, or was skipped | use a new key |
| `outcome_unknown` | the write may have reached the provider | do not retry; reconcile or check the timeline |
| `duplicate` / `forbidden` / `rate_limited` / `not_found` | the provider's reason, in `detail` | duplicate: change the text; rate_limited: wait |
| `api_error` | anything else from the provider, a network failure before sending, an unreadable refresh response, an unexpected error | retry later; report if it persists |
