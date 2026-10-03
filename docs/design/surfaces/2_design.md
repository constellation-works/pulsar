---
title: Surfaces — Design
owner: claude
last_updated: 2026-10-03
last_validated: 2026-10-03
status: Accepted
feature: surfaces
doc_role: design
type: design
summary: The MCP tools and the caller boundary, the operator CLI, and how the Orbit plugin runs (home, settings, paths, grants, launcher, envelope).
tags: [surfaces, mcp, cli, orbit-plugin]
paths: ["src/pulsar/**", ".orbit-plugin/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-13029, ORB-13032, ORB-13114, ORB-13115, ORB-13138, ORB-13727, ORB-13730, ORB-13751, ORB-13772]
---

# Surfaces — Design

The three front ends as built. The phase 4 plugin tools are in [3_vision.md](./3_vision.md).

## 1. MCP Server

`pulsar serve` ([mcp/server.py](../../../src/pulsar/mcp/server.py), over the tools in
[app/tools.py](../../../src/pulsar/app/tools.py)) runs over stdio, or
streamable HTTP on a loopback address (`--transport http --host 127.0.0.1 --port 8977`;
`--host` accepts only `127.0.0.1`, `localhost` or `::1`; both flags are refused without
`--transport http`). Over HTTP a request whose `Host` is not one of the bound loopback
`name:port` authorities, or whose `Origin` is not `http://` plus one of them, is refused
(421, 403) before any tool runs; a request with no `Origin` is accepted, for MCP clients
([decision](./4_decisions.md#a-post-without-an-origin-is-an-mcp-client-not-a-browser)). Register it with a client, for
example `claude mcp add pulsar -- uv --directory /path/to/pulsar run pulsar serve`.

| Tool | Annotation | Provider call | Input → output |
|---|---|---|---|
| `whoami` | read-only | `GET /2/users/me` (cached) | `account?` → `{user_id, username}` |
| `validate_post` | read-only | none | `text`, `reply_to_post_id?`, `quote_post_id?` → `{text, weighted_length, has_url, estimated_cost_usd}` |
| `validate_plan` | read-only | none | `plan`, `account?` → per account `{account, digest, estimated_cost_usd, posts}` |
| `create_post` | publishes | `POST /2/tweets` | `text`, `reply_to_post_id?`, `quote_post_id?`, `media_ids?`, `idempotency_key?`, `account?`, `caller?`, `dry_run?` → `{post_id, url, text}` |
| `upload_media` | publishes | chunked `/2/media/upload` | `path` or `base64`, `mime?`, `account?`, `caller?` → `{media_id}` |
| `delete_post` | destructive | `DELETE /2/tweets/:id` | `post_id`, `idempotency_key?`, `account?`, `caller?` → `{post_id, deleted}` |

- `create_post` is a one-post plan through the publisher: policy, idempotency and the secret
  scanner apply exactly as for `pulsar publish`. `dry_run: true` predates `validate_post`,
  returns the same report and records nothing.
- Upload media as the account that will post it.
- Every tool returns an object: `{ok: true, …}` or `{ok: false, code, message, retryable,
  detail?}`. An unexpected exception becomes a non-retryable `internal` naming its type; nothing
  raises into the client.
- Arguments are strict: every input schema has `additionalProperties: false`, and an unknown
  argument is refused, not ignored. `create_post` always returns `replayed`.
- Planning, media reads and claims run in a worker thread so one slow call does not stall the
  server; the publisher's per-post ledger writes stay on the loop (see
  [Publishing — Decisions](../publishing/4_decisions.md)).

### The caller boundary

pulsar cannot decide whether a post should go out; it sees a tool call, not the conversation.
Intent enforcement belongs to the caller: the harness, the MCP client's permission layer, a
routine's guard, and (phase 4) approvals. pulsar makes the boundary legible to a policy layer
that gates by tool name and annotations:

- `whoami`, `validate_post`, `validate_plan`: `readOnlyHint: true`; safe to auto-allow.
- `create_post`, `upload_media`: `readOnlyHint: false, destructiveHint: false`; prompt.
- `delete_post`: `destructiveHint: true`; prompt harder.

`create_post(dry_run=true)` shares a name with a live post, so a name-based gate cannot tell
them apart; new callers validate with `validate_post` or `validate_plan`. A dry run makes the
checks the live call would make before sending (schedule, idempotency key, budget and caps)
and records nothing.

`caller` (argument, else `PULSAR_CALLER`) is recorded in the ledger and is advisory: nothing
authenticates it.

## 2. Operator CLI

[cli/](../../../src/pulsar/cli/), verbs in [ops.py](../../../src/pulsar/app/ops.py),
health in [health.py](../../../src/pulsar/app/health.py). The entry point `pulsar/main.py`
builds a `LocalApp` and runs the CLI with it; `cli/main.py` builds the command tree and dispatches; each command declares its parser and holds its handler in `src/pulsar/cli/commands/`;
beneath them, `src/pulsar/cli/toolkit/` holds what they are written against: `context.py` prints a payload and `errors.py` a failure.

- **Help.** `pulsar` or `pulsar auth` alone prints that level's help on stderr and exits 2.
  The root help lists the commands in groups (Accounts, Publish, Observe, Maintenance,
  Services), built from each command's own declaration. `-V` prints the version.
- **Output.** Each command builds one payload. [render.py](../../../src/pulsar/cli/toolkit/render.py)
  resolves the mode once: `--format auto|table|json` or `--json`, accepted before or after
  the command; else `PULSAR_FORMAT`; else `auto`. An unknown `PULSAR_FORMAT` counts as
  `auto`, and `--json` with another `--format` is a usage error.
  - `auto` means `table` on a terminal and the piped form otherwise.
  - `table` renders the command's view ([views.py](../../../src/pulsar/cli/toolkit/views.py)):
    borderless tables with one line per record, `-` for absent values and right-aligned
    numbers, plus key-value fields for single results.
    - Values are cut with `…` only to fit a known width (`COLUMNS`, else the terminal's).
    - Color only on a terminal, never with `NO_COLOR` or `TERM=dumb` (`CLICOLOR_FORCE`
      overrides the latter). It marks states and never carries meaning alone.
  - The piped form prints tables as tab-separated lines with no header and fields as
    `label: value` lines, with no escapes and no truncation.
  - `json` prints the payload as one document; these documents are the machine contract
    (goldens in `tests/goldens/cli`).
  - Notices go to stderr in every mode: an empty result, the payload's
    `note`, an account's remedy.
- **Errors.** Nothing on stdout.
  - On stderr, `error: <message>`; in JSON mode, one object `{error, code, retryable,
    detail}` instead.
  - A usage error adds the usage line and a `--help` pointer. It is JSON (`invalid_argument`,
    `detail.usage`) when the arguments or `PULSAR_FORMAT` ask for JSON.
  - Exit codes: 0 success; 1 when the command failed or reported something not healthy or
    not settled; 2 for a usage error.
  - From 0.2.0, `publish --yes` and `auth status --offline` are usage errors naming
    their replacements: `--confirm` and `auth status` (offline by default). They are
    hidden from help and refused during parsing, before any command runs.
  - An unexpected exception is `internal`. A closed stdout (`pulsar history | head -1`)
    exits 0.
- **Effects.** The reports (`status`, `history`, `validate`, `auth status`) read the home
  without writing, creating or migrating anything. A write command upgrades the ledger schema
  and moves phase 1 credentials on first use; `pulsar migrate --confirm` does it on purpose.
  What sends, deletes or cannot be undone needs `--confirm` (`publish`, `auth logout`,
  `import-posted`, `migrate`, `auth migrate`), checked before anything else runs; without it
  `publish`, `import-posted` and both migrates report what they would do. `reconcile`
  applies without it ([decision](4_decisions.md#reconcile-and-first-use-migration-apply-without---confirm)).
  Writes name the `home` they wrote. A remedy that names a command pins it to that home
  (`PULSAR_HOME=… pulsar …`), so the plugin's advice acts on the plugin's home.

| Command | Network | What it does |
|---|---|---|
| `pulsar auth login | status | logout | migrate` | see [Accounts — Design](../accounts/2_design.md) | bind, check, unbind, migrate accounts |
| `pulsar status [--account A]` | none | budget and cap use, quiet hours, unresolved keys |
| `pulsar history [--account A] [--limit N]` | none | newest ledger rows with items; `total` and `truncated` (`--limit` 1–100, refused outside) |
| `pulsar validate PLAN.yaml [--account A]` | none | the publisher's report; needs no credentials |
| `pulsar publish PLAN.yaml [--account A] [--idempotency-key K] [--caller C] --confirm` | posts | prepares every account's plan, then publishes; without `--confirm` it makes every offline check the live run makes (key, caller, schedule, budget, cap) and sends nothing |
| `pulsar approve PLAN.yaml [--account A] [--workspace DIR] [--ttl 72h]` | none | prints every post in full, the cost and each account's digest to stderr, then records an approval once the human types the digest's first 8 characters; a stdin that is not a terminal is `interactive_only` and records nothing; refuses a plan changed since it was shown ([Engagement §2](../engagement/2_design.md#2-approvals)) |
| `pulsar approvals [--account A] [--limit N]` | none | approvals newest first, each with its state |
| `pulsar revoke ID` | none | revokes an approval; what was published under it stays |
| `pulsar reconcile [--account A]` | timeline reads only when something is unresolved | settles unknown and stale posts; one failing row is reported and the rest still run |
| `pulsar import-posted FILE [--account A] --confirm` | `/users/me` only if identity is not cached | imports `posted.jsonl`; without `--confirm` reports only |
| `pulsar migrate [--confirm]` | none | reports, or with `--confirm` applies, the ledger schema upgrade and the phase 1 credential move |
| `pulsar serve` | — | the MCP server |
| `pulsar orbit-tool` | — | the Orbit backend, one envelope on stdin |

## 3. Orbit Plugin

The tool-specific manifest (schemaVersion 2, namespace `pulsar`, exec backend, requires Orbit `>=0.24.0
<1.0.0`, platforms linux and macos, program `uv`). Three offline tools and four that spend
money:

| Tool | CLI | Output |
|---|---|---|
| `pulsar.status` | `orbit pulsar status` | per account: `health` (`healthy`, `unverified`, `unhealthy`, offline), budget and cap use, unresolved writes, last publication; `healthy`, `ready`, `attention`, effective `default_account` |
| `pulsar.validate` | `orbit pulsar validate PLAN.yaml` | inline `plan` or workspace `source`; `{valid: true, accounts}` (for a `source`, each with its `approve_command`) or `{valid: false, error}` |
| `pulsar.history` | `orbit pulsar history` | newest ledger rows flattened for a table (`limit` 1–100), with `total` and `truncated`; `keys` (1–100) returns the rows held under those idempotency keys instead, however old |
| `pulsar.engagements` | — | others' posts mentioning the account (`hours` 1–168, `limit` 1–100), each with `replied`; a paid read |
| `pulsar.metrics` | — | the account's own posts (`days` 1–30, `limit` 1–100) with their metrics and `totals`; a paid read |
| `pulsar.publish` | — | publishes a workspace plan `source` under a human approval of each account's digest; `dry_run` checks only |
| `pulsar.dispatch` | — | reconciles unknown writes, then publishes the approved, due plans `[dispatch] plans` matches (`max_publish` 1–10); `dry_run` reports only |

`status.ready` gates the explicit `account`, else the effective `default_account`:
the configured default, else the sole non-revoked bound account. With zero or several
bound accounts and no configured default, `default_account` stays null and `ready` is
false; account-less validate and publish still return their existing account-selection
errors. Filtering status by `account` does not change the reported default.
`ready` is false for an unhealthy selected account, its unresolved writes, or any legacy
credentials awaiting migration. An unrelated account's health or unresolved writes do
not block the selected account. `healthy` still summarises every reported account with
no attention, and `health` remains three-valued. Unverified health can be `ready: true`:
the first authorized live call settles identity or token refresh. This is local evidence,
not proof that refresh will succeed, and no approval, budget, cap or quiet-hours check is
bypassed. An unregistered configured default cannot be ready.

The engager, post-proposer, weekly-report and x-updates templates and the publishing
skill gate on `ready` and stop with the returned error and attention if a later live
call fails with `auth_expired` or `reauth_required`. The auth-health helper suppresses
only routine expiry with a stored refresh token and matching cached identity; other
unverified reasons still raise human attention ([Accounts §8](../accounts/2_design.md#8-offline-auth-health-alarm)).

The first three are `execution_kind: read_only` and read the home without writing to it. The
last four are `mutating`, so Orbit lets an agent call them only from a task whose
`required_tools` names them ([Engagement — Design §3](../engagement/2_design.md#3-plugin-tools)).
All are `mcp_scope: workspace`, with request and response schemas under
[schemas/](../../../.orbit-plugin/schemas/). An input key the request schema does not list is
refused (`invalid_argument` naming it). Panels: *Pulsar accounts* (kv, `status`) and
*Recent publications* (table, `history`). Auto-tasks: `auth-health`, `engager`, `post-proposer`,
`weekly-report` and `x-updates`, seeded disabled ([Engagement — Design §4](../engagement/2_design.md#4-auto-tasks)).
Routine: `dispatch`, seeded disabled as `pulsar-dispatch`, with the activity and job it fires
([Engagement — Design §5](../engagement/2_design.md#5-scheduled-dispatch)). The skill [skills/publish](../../../.orbit-plugin/skills/publish/SKILL.md) is linked as `pulsar-publish`:
checking accounts, reading engagement, drafting and validating a plan, the approve-then-publish
handoff, and, in
[references/setup.md](../../../.orbit-plugin/skills/publish/references/setup.md), walking a human through
installing pulsar and binding an account.

### How it runs

- **Home.** `$ORBIT_PLUGIN_STATE/home` (`~/.orbit/state/plugins/pulsar/home`): the sandbox can
  write only the plugin state. The CLI and MCP server reach the same home with `PULSAR_HOME`;
  a `PULSAR_HOME` under Orbit that names any other home is `invalid_config` before any work,
  since the call could not use it. Outside Orbit
  (`pulsar orbit-tool` for debugging) the usual home applies.
- **Settings.** The home's `config.toml` is the only source. `[plugins.pulsar]` has a closed,
  empty schema; a call that carries keys is `invalid_argument`.
- **Paths.** `source` and plan media resolve against the workspace root and must stay inside it,
  symlinks included; the source is opened without following a symlink, so a swap after the
  check cannot redirect the read. Configured media roots are replaced by the workspace for
  plugin calls. The backend never changes its working directory.
  A source is at most 256 KiB of UTF-8; an unreadable source is a tool error, a bad plan is
  `valid: false`.
- **Grants.** `fs` (read `{{workspace}}`, write `{{plugin_state}}`) and `network: any` (X, and
  the first call's dependency sync). Install with path-scoped grants:
  `orbit plugin add <export> --enable --grant 'fs={{workspace}},{{plugin_state}}' --grant network`.
- **Launcher.** [bin/pulsar](../../../.orbit-plugin/bin/pulsar) keeps the venv, uv cache, uv-managed Python,
  bytecode and temp files under the plugin state. It runs `uv sync --frozen --no-dev
  --no-install-project` once per `uv.lock` checksum, then execs the venv's Python with
  `PYTHONPATH` at the plugin's generated `src/`. Root `src/`, `pyproject.toml` and `uv.lock`
  are canonical; `make plugin` refreshes their committed copies inside `.orbit-plugin/`, and
  `tests/test_plugin_package.py` detects drift. The canonical `pyproject.toml` declares an
  explicit uv workspace with no additional members, so the generated plugin stops project
  discovery at its own root. Without that boundary, uv inspects the enclosing checkout's
  `pyproject.toml` outside the conformance sandbox's read grants and fails before syncing
  ([ORB-13751]). Warm calls still skip uv. `requires.programs: [uv]` lets Orbit resolve uv at
  enable ([ORB-13032]); older hosts need uv on the caller's `PATH`.
- **Envelope.** [orbit/backend.py](../../../src/pulsar/orbit/backend.py) reads
  `{schema_version: 1, tool, input, context}` and writes exactly one JSON line,
  `{ok: true, output}` or `{ok: false, error: {code, message, retryable, detail?}}`, exit 0
  ([decision](./4_decisions.md#pulsar-orbit-tool-exits-0-when-it-answered)). It never raises; an unexpected exception is
  `internal`. Diagnostics go to stderr.
- **Install tree.** Orbit selects `.orbit-plugin/` from a source checkout or export and
  installs only that directory. It contains the manifest, launcher, schemas, skill,
  definitions, conformance cases and generated runtime package. The installer refuses
  symlinks in that directory. Export a commit containing the refreshed generated copy.

### Conformance

`.orbit-plugin/tests/conformance/pulsar.yaml` holds goldens for `orbit plugin test .`, which
runs them through Orbit's sandbox on a build with the dedicated plugin-root layout.
`tests/test_orbit_tool.py` runs the same
goldens in-process in `make check`, validates every output against its schema, and checks that
no request schema has a credential-shaped property.

## 4. Concerns & Honest Limitations

- **Orbit drops `retryable` and `detail`.** Orbit 0.24 passes only `code` and `message` of a
  plugin error on to the caller ([ORB-13114]); `validate` therefore reports plan errors as a
  result, so there are two error shapes.
- **No host-attested caller.** The plugin context has no task or run id ([ORB-13115]); the
  caller on plugin writes would be as self-asserted as on MCP.
- **First call needs network and time.** The dependency sync takes about 10 s; `orbit plugin
  test` appears to sync per case (not verified).
- **The plugin home is not yet private.** Until the host's Orbit carries [ORB-13008], plugin
  state is readable by other plugin backends and agent sandboxes.
- **Three surfaces, three argument styles.** MCP tools take flat arguments, the CLI takes plan
  files, the plugin takes plans or workspace sources; only the core is shared.

## Task References

- [ORB-13008] — Orbit: private plugin state (ws_orbit).
- [ORB-13029] — built the Orbit plugin.
- [ORB-13032] — Orbit: resolve and grant `requires.programs` (ws_orbit).
- [ORB-13114] — Orbit: keep `retryable` and `detail` in plugin errors (ws_orbit).
- [ORB-13115] — Orbit: task and run id in the plugin context (ws_orbit).
- [ORB-13138] — aligned the surfaces with the constellation standards.
- [ORB-13727] — added `pulsar.dispatch` and the `pulsar-dispatch` routine.
- [ORB-13730] — retired the deprecated CLI flags with usage errors in 0.2.0.
- [ORB-13630] — moved the plugin to `.orbit-plugin/` with a checked generated runtime copy.
- [ORB-13751] — bounded uv project discovery to the plugin root so sandboxed cold syncs succeed.
- [ORB-13772] — added the selected-account readiness gate and effective default and migrated scheduled callers and the publishing skill.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
