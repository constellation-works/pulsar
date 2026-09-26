---
title: Surfaces — Design
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: surfaces
doc_role: design
type: design
summary: The MCP tools and the caller boundary, the operator CLI, and how the Orbit plugin runs (home, settings, paths, grants, launcher, envelope).
tags: [surfaces, mcp, cli, orbit-plugin]
paths: ["src/pulsar/surfaces/**", "plugin.yaml", "bin/pulsar", "schemas/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-13029, ORB-13032, ORB-13114, ORB-13115]
---

# Surfaces — Design

The three front ends as built. The phase 4 plugin tools are in [3_vision.md](./3_vision.md).

## 1. MCP Server

`pulsar serve` ([mcp.py](../../../src/pulsar/surfaces/mcp.py)) runs over stdio, or
streamable HTTP bound to loopback (`--transport http --port 8977`; exposing it and putting
auth in front is an operator step). Register it with a client, for example
`claude mcp add pulsar -- uv --directory /path/to/pulsar run pulsar serve`.

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
  detail?}`. An unexpected exception becomes a non-retryable `api_error` naming its type; nothing
  raises into the client.

### The caller boundary

pulsar cannot decide whether a post should go out; it sees a tool call, not the conversation.
Intent enforcement belongs to the caller: the harness, the MCP client's permission layer, a
routine's guard, and (phase 4) approvals. pulsar makes the boundary legible to a policy layer
that gates by tool name and annotations:

- `whoami`, `validate_post`, `validate_plan`: `readOnlyHint: true`; safe to auto-allow.
- `create_post`, `upload_media`: `readOnlyHint: false, destructiveHint: false`; prompt.
- `delete_post`: `destructiveHint: true`; prompt harder.

`create_post(dry_run=true)` shares a name with a live post, so a name-based gate cannot tell
them apart; new callers validate with `validate_post` or `validate_plan`.

`caller` (argument, else `PULSAR_CALLER`) is recorded in the ledger and is advisory: nothing
authenticates it.

## 2. Operator CLI

[cli.py](../../../src/pulsar/surfaces/cli.py), verbs in [ops.py](../../../src/pulsar/surfaces/ops.py).
Each prints JSON and exits 0 on success, 1 otherwise.

| Command | Network | What it does |
|---|---|---|
| `pulsar auth login | status | logout | migrate` | see [Accounts — Design](../accounts/2_design.md) | bind, check, unbind, migrate accounts |
| `pulsar status [--account A]` | none | budget and cap use, quiet hours, unresolved keys |
| `pulsar history [--account A] [--limit N]` | none | newest ledger rows with items |
| `pulsar validate PLAN.yaml [--account A]` | none | the publisher's report; needs no credentials |
| `pulsar publish PLAN.yaml [--account A] [--idempotency-key K] [--caller C] --yes` | posts | publishes; without `--yes` only validates |
| `pulsar reconcile [--account A]` | timeline reads only when something is unresolved | settles unknown and stale posts |
| `pulsar import-posted FILE [--account A]` | `/users/me` only if identity is not cached | imports `posted.jsonl` |
| `pulsar serve` | — | the MCP server |
| `pulsar orbit-tool` | — | the Orbit backend, one envelope on stdin |

## 3. Orbit Plugin

`plugin.yaml` (schemaVersion 2, namespace `pulsar`, exec backend, requires Orbit `>=0.24.0
<1.0.0`, platforms linux and macos, program `uv`). Phase 3 ships read-only tools:

| Tool | CLI | Output |
|---|---|---|
| `pulsar.status` | `orbit pulsar status` | per account: token health (offline), budget and cap use, unresolved writes, last publication; `healthy` and `attention` |
| `pulsar.validate` | `orbit pulsar validate PLAN.yaml` | inline `plan` or workspace `source`; `{valid: true, accounts}` or `{valid: false, error}` |
| `pulsar.history` | `orbit pulsar history` | newest ledger rows flattened for a table (`limit` 1–100) |

All are `execution_kind: read_only`, `mcp_scope: workspace`, with request and response
schemas under [schemas/](../../../schemas/). Panels: *Pulsar accounts* (kv, `status`) and
*Recent publications* (table, `history`). The skill [skills/publish](../../../skills/publish/SKILL.md)
is linked as `pulsar-publish`.

### How it runs

- **Home.** `$ORBIT_PLUGIN_STATE/home` (`~/.orbit/state/plugins/pulsar/home`): the sandbox can
  write only the plugin state. The CLI and MCP server reach the same home with `PULSAR_HOME`.
  Outside Orbit (`pulsar orbit-tool` for debugging) the usual home applies.
- **Settings.** The home's `config.toml` is the only source. `[plugins.pulsar]` has a closed,
  empty schema; a call that carries keys is `invalid_argument`.
- **Paths.** `source` and plan media resolve against the workspace root and must stay inside it,
  symlinks included. Configured media roots are replaced by the workspace for plugin calls.
  A source is at most 256 KiB of UTF-8; an unreadable source is a tool error, a bad plan is
  `valid: false`.
- **Grants.** `fs` (read `{{workspace}}`, write `{{plugin_state}}`) and `network: any` (X, and
  the first call's dependency sync). Install with path-scoped grants:
  `orbit plugin add <export> --enable --grant 'fs={{workspace}},{{plugin_state}}' --grant network`.
- **Launcher.** [bin/pulsar](../../../bin/pulsar) keeps the venv, uv cache, uv-managed Python,
  bytecode and temp files under the plugin state. It runs `uv sync --frozen --no-dev
  --no-install-project` once per `uv.lock` checksum, then execs the venv's Python with
  `PYTHONPATH` at the plugin's `src/`. `requires.programs: [uv]` lets Orbit resolve uv at
  enable ([ORB-13032]); older hosts need uv on the caller's `PATH`.
- **Envelope.** [orbit_tool.py](../../../src/pulsar/surfaces/orbit_tool.py) reads
  `{schema_version: 1, tool, input, context}` and writes exactly one JSON line,
  `{ok: true, output}` or `{ok: false, error: {code, message, retryable, detail?}}`, exit 0.
  It never raises. Diagnostics go to stderr.
- **Install tree.** The installer refuses symlinks anywhere in the tree (so `CLAUDE.md` is a
  file containing `@AGENTS.md`), and a working tree carries a `.venv`; install from `git
  archive` of a commit.

### Conformance

`tests/conformance/pulsar.yaml` holds goldens for `orbit plugin test <export> --grant
fs,network`, which runs them through Orbit's sandbox. `tests/test_orbit_tool.py` runs the same
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

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
