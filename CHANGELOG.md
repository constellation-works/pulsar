# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **x-updates auto-task.** The retired x-updates routine as a plugin auto-task, seeded
  disabled: it drafts approval-gated plans for new constellation-works releases, newly
  public repositories and notable merged pull requests, skipping what the ledger (imported
  history included) or an earlier draft already holds. It never publishes.
- **Plan `key`.** A plan may name its idempotency key (`release:<repo>:<tag>`, ...);
  `pulsar publish` and `pulsar.publish` publish under it, so a second draft of the same
  announcement replays or conflicts instead of posting again.
- **`pulsar.history` `keys`.** Looks up the rows held under given idempotency keys.

## [0.1.0] - 2026-09-27

The first release: agents publish to X as accounts a human bound on the host, without ever
holding a credential.

### Added

- **Publishing.** One publisher behind every surface. A plan (YAML) is a post or a thread,
  with replies, quotes and media with alt text. Each publish runs offline checks, the secret
  scanner, and the budget ($/day, $/month), daily post cap and quiet hours policy before
  anything is sent.
- **Ledger.** SQLite `ledger.sqlite3` in the pulsar home, written before each network call, so
  a retry replays its receipt instead of posting (and paying) twice. A thread that fails
  midway resumes where it stopped. `pulsar reconcile` settles posts whose outcome is unknown.
  `pulsar history` lists recent writes, `writes.jsonl` exports them, and
  `pulsar import-posted` imports an existing `posted.jsonl`.
- **Accounts.** `pulsar auth login | status | logout`: OAuth 2.0 with PKCE. X confirms the
  token's handle before it is stored, encrypted, in the home. Tokens refresh under a
  cross-process lock. A post is sent only with the login whose identity was checked.
- **Approvals.** `pulsar approve` shows a plan in full and records an approval of its digest
  once a human types the digest's first 8 characters (terminal only).
  `pulsar approvals` and `pulsar revoke` manage them. The Orbit plugin publishes an agent's
  draft only under an approval; no tool on any surface can record one.
- **Engagement reads**, on the same budget as posts: the account's mentions, each marked if
  already answered, and its own posts' metrics. Nothing read is stored.
- **CLI** `pulsar`: tables on a terminal, tab-separated lines when piped, `--json` for one
  JSON document. `pulsar migrate` brings a home up to date after an upgrade.
- **MCP server** `pulsar serve` (stdio): `whoami`, `validate_post`, `validate_plan`,
  `create_post`, `upload_media`, `delete_post`.
- **Orbit plugin**: `pulsar.status`, `validate` and `history`; and, from tasks that require
  them, `engagements`, `metrics` and `publish`. Three auto-tasks are seeded switched off:
  `engager`, `post-proposer` and `weekly-report`.
- **Safeguards.** Media paths are confined to configured roots. Chunked media uploads have
  an overall deadline, capped below the plugin's timeout. Provider error text is bounded and
  secret-masked.
- **Development.** `make check` (lock, ruff, basedpyright strict, offline pytest);
  `make audit` (advisories, yanked pins and licenses in `uv.lock`). CI runs both on
  `agent-main`.

### Deprecated

- `pulsar publish --yes`: alias for `--confirm`. Accepted with a warning in 0.1.x; a usage
  error from the next minor release.
- `pulsar auth status --offline`: a no-op (`pulsar auth status` is offline by default).
  Accepted with a warning in 0.1.x; a usage error from the next minor release.

[Unreleased]: https://github.com/constellation-works/pulsar/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/constellation-works/pulsar/releases/tag/v0.1.0
