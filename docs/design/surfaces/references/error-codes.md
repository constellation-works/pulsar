---
type: design
summary: "Reference: error codes shared by every pulsar surface"
last_validated: 2026-09-27
---

# Reference: Error Codes

Every surface reports failures with these codes. MCP: `{ok: false, code, message, retryable,
detail}` (`detail` null when there is none). Orbit: `{code, message, retryable, detail?}` under
`error`. CLI: `{error, code, retryable, detail}` on stderr (`error` is the message), exit 1, or
2 for a usage error. `retryable` is true only
when repeating the identical call later can succeed. Codes are defined in
[internal/errors/codes.py](../../../../src/pulsar/internal/errors/codes.py).

| code | meaning | what to do |
|---|---|---|
| `auth_expired` | no account bound, logged out, no token, refresh failed (account then `reauth_required`), or legacy credentials needing `pulsar auth migrate --confirm` | stop; a human runs what `message` says |
| `account_mismatch` | the credentials belong to another handle than the alias's or `expected_handle` (`detail: {alias, expected_handle, bound_handle}`); nothing sent | stop; a human re-runs `pulsar auth login` as the right account |
| `unknown_account` | the alias is not registered; `detail.known` lists those that are | use a known alias, or have a human bind it |
| `insecure_storage` | pulsar state it would trust is a symlink, not owned by the user, or too wide: the home or an account directory wider than 0700; `key` / `tokens.enc` / `accounts.json` wider than 0600; `config.toml` or `client.json` writable by group/other; or a corrupt `key` | stop; a human runs the `chmod` in `detail.fix` |
| `invalid_config` | `config.toml` has an unknown key or bad value, `accounts.json` is unreadable, the ledger is from a newer pulsar, a `path` upload with no media roots, or (Orbit backend) a `PULSAR_HOME` that is not the plugin's home | fix the file or variable named in `message` |
| `invalid_text` | empty, over the length limit, control characters | rewrite |
| `invalid_argument` | a malformed key, alias or id, no `account` while several are bound and none is default, a bad tool input | fix the argument |
| `invalid_plan` | a plan's shape is wrong (`detail.at` names where), including a reply and a quote together | fix the plan |
| `invalid_media` | bad path or base64, outside the roots, not a regular file, a type that is not allowed or disagrees with its claim (`detail: {declared, sniffed}`), oversized, or failed video processing | fix the input |
| `secret_detected` | text, alt, media or key matches a credential pattern | rewrite; never retry verbatim |
| `unsupported` | the provider cannot do what the plan asks (thread, reply, quote) | change the plan |
| `not_due` | the plan's `not_before` is in the future (`detail.retry_after`) | publish after then |
| `quiet_hours` / `daily_cap` / `budget_exceeded` | the policy refused; nothing sent, no row (`detail.retry_after`) | wait; `retryable: false` means it can never pass on its own |
| `approval_required` | a publish that needs a human approval has none in force for this digest and account (`detail: {account, digest, last_approval}`: none, `revoked`, `used`, `expired`); nothing sent, no row | stop; a human reads the plan and runs `pulsar approve` at a terminal |
| `interactive_only` | `pulsar approve` was run without a terminal on stdin; nothing recorded | a human runs it at a terminal |
| `idempotency_conflict` | the key was used for a different request, tool or account, or was skipped | use a new key |
| `outcome_unknown` | the write may have reached the provider | do not retry; reconcile or check the timeline |
| `duplicate` / `forbidden` / `rate_limited` / `not_found` | the provider's reason, in `detail` | duplicate: change the text; rate_limited: wait |
| `api_error` | anything else from the provider, a network failure before sending, an unreadable refresh response | retry later; report if it persists |
| `upload_timeout` | X media `initialize` through `finalize` exceeded 30 seconds plus media size / 256 KiB/s, or the Orbit publish deadline arrived; no post was sent and the publisher records the item failed | retry later (`retryable: true`); the prior upload may leave an unused media id |
| `lock_timeout` | another pulsar process held a lock past the deadline (`detail.holder`: `pid`, `label`, `acquired_at`) | retry later; if it persists, check that process |
| `credentials_unreadable` | stored credentials exist but cannot be read: wrong or replaced `key`, a corrupt bundle, or one written by a newer pulsar | stop; a human restores the `key` or re-runs `pulsar auth login` |
| `internal` | a bug or unexpected failure inside pulsar (`message` names the exception type) | report it; do not retry blindly |
