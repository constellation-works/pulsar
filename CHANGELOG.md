# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **x-updates busy-week scans.** Raw collection no longer stops at 100 candidates;
  editorial filtering keeps releases and newly public repos ahead of the newest notable PRs
  within the 100-candidate lookup limit. PR candidates include labels and author for review.

## [0.2.0] - 2026-10-03

### Added

- **Bluesky channel.** Plans publish posts and threads, replies, quotes and images with
  alt text to `bsky:<handle>` accounts through the same publisher, approvals, ledger and
  policy as X. Provider checks include grapheme lengths, rich-text link facets and media
  limits; identity, delete, reconcile, mentions and own-post metrics use the channel boundary.
  Multi-account plans can target X and Bluesky, with provider-specific variants.
- **Bluesky login.** `pulsar auth login --account bsky:<handle>` signs in through atproto
  OAuth (PAR, PKCE S256, DPoP-bound tokens, `use_dpop_nonce` retries) and stores the tokens
  and a per-account DPoP key, encrypted, only after the token's DID resolves back to the
  handle. `auth status [--live]`, `auth logout`, refresh under the cross-process lock and
  `pulsar.status` work as for X. The client is the loopback development client unless
  `[oauth.bsky] client_id` names a hosted client-metadata document.
- **auth-health auto-task.** A disabled daily offline check of all bound accounts proposes
  deduplicated human attention tasks for non-routine unverified or unhealthy logins. It never refreshes,
  logs in or publishes; the task carries the account's exact verify or re-authorize command.
- **x-updates auto-task.** The retired x-updates routine as a plugin auto-task, seeded
  disabled: it drafts approval-gated plans for new constellation-works releases, newly
  public repositories and notable merged pull requests, skipping what the ledger (imported
  history included) or an earlier draft already holds. It never publishes.
- **Plan `key`.** A plan may name its idempotency key (`release:<repo>:<tag>`, ...);
  `pulsar publish` and `pulsar.publish` publish under it, so a second draft of the same
  announcement replays or conflicts instead of posting again.
- **`pulsar.history` `keys`.** Looks up the rows held under given idempotency keys.
- **Scheduled dispatch.** `pulsar.dispatch` reconciles unknown writes and publishes only
  approved, due plans found through `[dispatch] plans` workspace-relative globs, with a
  bounded batch and an offline dry run. The deterministic `pulsar-dispatch` routine runs
  every five minutes without a model; it is seeded disabled until a human enables it.

### Changed

- **Offline readiness.** `pulsar.status` exposes `ready` for the selected account while
  preserving three-valued health and `healthy`. Its `default_account` reports the configured
  default or, when unset, the sole non-revoked bound account.
- **Plugin layout.** The installable plugin now lives in `.orbit-plugin/`, with its manifest
  at `.orbit-plugin/plugin.yaml`, launcher, schemas, skills, definitions and conformance
  goldens. Root `src/`, `pyproject.toml` and `uv.lock` remain canonical; `make plugin`
  refreshes the committed runtime copies. Orbit installs only that directory.
- **Recorded engagement decisions.** Standing publishing policies remain declined until
  caller identity can be attested; each agent draft still needs a human approval. Scheduled
  dispatch ships disabled. Budget defaults remain $1/day, $10/month and five posts/day, with
  no quiet window and the existing $0.005 estimated X read price pending human verification.
- **Task delivery.** Orbit delivers task work as pull requests against `agent-main`.

### Removed

- `pulsar publish --yes` is a usage error (exit 2); use `--confirm`.
- `pulsar auth status --offline` is a usage error (exit 2); use `pulsar auth status`,
  which is offline by default. Both flags are hidden from help and refused before any work.

### Fixed

- **x-updates public GitHub fallback.** Scans prefer authenticated `gh` and otherwise
  use unauthenticated public REST without extra credentials or grants. Rate limits and
  network failures report a partial scan and stop drafting instead of reporting nothing new.
- **Scheduled token expiry.** The auto-tasks and publishing skill gate on readiness so
  routine access-token expiry with a stored refresh token can reach the first authorized
  live call. Auth-health no longer files verification tasks for that state with a matching
  cached identity; unhealthy accounts and other unverified reasons still raise attention.
- **Unset default account.** Status reports the same sole-account fallback that account-less
  validate and publish plans resolve to, without requiring a config file.
- **Sandboxed media validation.** Extension claims no longer initialize the host MIME
  database, avoiding a denied `/etc/mime.types` read for workspace-contained MP4s and
  images. Confinement, content checks, secret scanning and approval digests are unchanged.
- **No-file auto-task delivery.** `engager`, `post-proposer` and `weekly-report` declare
  `no-diff-expected`, so their documented paths that write no files can deliver task evidence.
- **Plugin test isolation.** Package layout checks inspect tracked and non-ignored untracked
  files, ignoring nested Orbit worktrees while still catching stray manifests in the checkout.
- **Dependency security.** Updated the locked PyJWT dependency from 2.13.0 to 2.15.1 to
  resolve the advisories reported by `make audit`.
- **Conformance sandbox dependency sync.** `[tool.uv.workspace] members = []` makes the
  generated plugin a standalone project, so uv does not read a parent project outside its
  sandbox grants during `orbit plugin test`.
- **Bluesky link facets.** Bare-domain detection uses an offline TLD list, avoiding links
  on file names such as `config.toml` and `plugin.yaml` while preserving explicit URLs.
- **Duplicate-image alt text.** Bluesky retains each occurrence's alt text when the same
  image bytes appear more than once in a post.
- **x-updates draft collisions.** Plan file names reversibly encode the entire key, so
  distinct announcement keys cannot overwrite each other's drafts.
- **Terminal-key replays.** Published and skipped keys replay without fresh approval or
  budget/quiet-hours admission; conflict checks still apply, and no second post is sent.

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

[Unreleased]: https://github.com/constellation-works/pulsar/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/constellation-works/pulsar/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/constellation-works/pulsar/releases/tag/v0.1.0
