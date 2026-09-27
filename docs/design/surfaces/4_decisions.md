---
title: Surfaces — Decisions
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-26
status: Accepted
feature: surfaces
doc_role: decisions
type: design
summary: Exec backend over mcp, one settings source, plan errors as results, workspace-confined plugin media, the launcher's shape, and CLI output modes.
tags: [surfaces, orbit-plugin, mcp]
paths: ["plugin.yaml", "bin/pulsar", "src/pulsar/orbit/backend.py", "src/pulsar/mcp/server.py", "src/pulsar/app/tools.py", "src/pulsar/app/plugin.py"]
related_features: [publishing, accounts]
related_artifacts: [ORB-12124, ORB-13029]
---

# Surfaces — Decisions

Non-obvious decisions about pulsar's front ends. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## Orbit runs pulsar as an exec backend, one process per call

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `plugin.yaml` (`backend.type: exec`), `src/pulsar/orbit/backend.py::main`

### Context

Orbit offers `exec` (a process per call) and `mcp` (a long-lived child per workspace and
allowlist). Every long-lived pulsar process holds token state and can refresh.

### Decision

Use `exec`. Each call opens the home, reads the ledger fresh, refreshes under the file lock if
it must, and exits.

### Consequences

- No extra long-lived refresher per workspace, and no cached state to go stale between calls.
- Cost: every call pays interpreter start-up (about 0.15 s warm, 0.45 s cold).

## `config.toml` is the only settings source

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `src/pulsar/orbit/backend.py::_parse`, `schemas/config.json`

### Context

Orbit can pass a validated `[plugins.pulsar]` section to the backend. The CLI and MCP server
read `config.toml` in the home. Budgets and caps are enforced against one shared ledger.

### Decision

The home's `config.toml` stays the single source. `[plugins.pulsar]` has a closed, empty
schema, and a call carrying keys is refused, not ignored.

### Consequences

- The plugin, the CLI and the MCP server can never enforce different budgets on one ledger.
- Cost: pulsar cannot be configured per workspace through Orbit, and the Orbit config UI shows
  nothing for it.

## `validate` reports an invalid plan as a result

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `src/pulsar/app/plugin.py::validate`

### Context

An agent drafting a post needs the error code and `detail` (`detail.at`, the weighted length)
to fix it. Orbit 0.24 passes only `code` and `message` of a plugin error on ([ORB-13114]).

### Decision

`pulsar.validate` returns `{valid: false, error: {code, message, retryable, detail}}` for a
plan that fails validation (`invalid_plan`, `invalid_text`, `invalid_media`,
`secret_detected`, `unsupported`: `orbit_tool.PLAN_VERDICTS`). Bad tool input (no plan, an
unreadable source, an `account` that is not bound or not the plan's) and a broken home
(`insecure_storage`, `invalid_config`, `lock_timeout`) stay tool errors.

### Consequences

- Drafting agents get the full detail through Orbit today.
- Cost: two error shapes on one surface; revisit when [ORB-13114] lands.

## Plugin calls confine media to the workspace

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `src/pulsar/orbit/backend.py::_settings`, `src/pulsar/app/plugin.py::read_source`

### Context

The plugin sandbox grants read access to the workspace only. Configured `media.roots` outside
it would be unreadable, and a relative path means nothing without a base.

### Decision

For plugin calls the media roots are replaced by the workspace root, relative paths resolve
against it, and a plan `source` must resolve inside it.

### Consequences

- The sandbox and pulsar's own confinement agree; errors name the workspace rule, not an
  opaque permission failure.
- Cost: media shared across workspaces (a marketing asset library) cannot be referenced from
  another workspace's plan.

## The launcher syncs once, then runs the venv directly

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `bin/pulsar`

### Context

The installer refuses symlinks, so the plugin tree cannot carry a venv, and the plugin root is
read-only. `uv run` has no `--no-install-project` and would install pulsar into the venv on
every call.

### Decision

`bin/pulsar` keeps the venv and uv's caches in the plugin state, runs `uv sync --frozen
--no-dev --no-install-project` only when `uv.lock`'s checksum changes, and execs the venv's
Python with `PYTHONPATH` at the plugin's `src/`.

### Consequences

- Warm calls skip uv entirely; a new plugin version re-syncs once.
- Cost: the first call after an install or upgrade needs network and about 10 s, and a
  dependency not in `uv.lock` is simply missing.

## The CLI keeps its verb-first grammar

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/cli/main.py::build_parser`

### Context

A common CLI shape is one order, `<tool> <noun> <verb>`, with verbs from a shared vocabulary. pulsar
mixes `pulsar auth login` (noun, verb) with `pulsar publish` and `pulsar reconcile` (verb
only), and its verbs (`publish`, `reconcile`, `import-posted`, `migrate`) are not `add`,
`list` or `show`. Routines, the skill and the operator's shell history already use them.

### Decision

Keep the tree as it is. The operator verbs act on the one ledger, which has no second noun to
tell apart; `auth` is the only resource with verbs of its own. New commands go under a noun.

### Consequences

- No caller breaks, and the command names say what they cost (`publish` posts).
- Cost: two orders in one tool; if a second resource grows verbs, regroup under nouns with
  the old names kept as aliases for a release.

## The CLI prints JSON only

**Recorded:** 2026-09-26 · [ORB-13138]
**Superseded by:** [The CLI renders for its reader, as orbit's does](#the-cli-renders-for-its-reader-as-orbits-does)
**Code anchors:** `src/pulsar/cli/toolkit/context.py::emit`

### Context

A CLI can render for a human on a terminal and in a piped form otherwise, with tables and
color rules. pulsar's callers are routines and agents; its one human command, `auth login`,
prints a short JSON receipt.

### Decision

Every command prints one JSON document on stdout, terminal or not. `--json` is accepted
everywhere as a no-op so callers that pass it work. Errors are one JSON
object on stderr; notices are prose lines on stderr. There is no mode to resolve, no
table and no color decision. Two outputs are argparse's, not
pulsar's: `--help` is wrapped to `COLUMNS` (the help goldens pin it to 100), and `--version`
prints the version as plain text.

### Consequences

- One output mode, so nothing to resolve, color or truncate; the piped form is JSON rather
  than tab-separated lines.
- Cost: an operator reads JSON at the terminal (`| jq` helps).

## The CLI renders for its reader, as orbit's does

**Recorded:** 2026-09-26 · [ORB-13248]
**Code anchors:** `src/pulsar/cli/toolkit/render.py::resolve_mode`, `src/pulsar/cli/toolkit/render.py::resolve_terminal`, `src/pulsar/cli/toolkit/views.py`, `src/pulsar/cli/toolkit/parser.py::CommandParser`

### Context

The JSON-only CLI answered a bare `pulsar` with a JSON usage error. Daniel judged that bad
UX for the operator at a terminal and asked for the CLI to behave like orbit's. pulsar has
not been released, and nothing parses its CLI output but its own tests. The skill and the
plugin go through `orbit-tool` and MCP.

### Decision

- Render output the way orbit does:
  - one payload per command;
  - a table or key-value view on a terminal, tab-separated lines when piped, and JSON on
    request (`--json`, `--format json` or `PULSAR_FORMAT=json`);
  - plain `error:` lines outside JSON;
  - grouped help, which a bare `pulsar` or `pulsar auth` prints, exiting 2.
- The JSON documents are unchanged and remain the machine contract.
- One exception remains: argparse wraps `--help` to the terminal width it looks up itself,
  outside `resolve_terminal`. The help goldens pin `COLUMNS=100`.

### Consequences

- An operator reads a table, and an agent or script passes `--json`, as with orbit.
- Cost:
  - a caller that relied on piped JSON must now pass `--json`;
  - every command needs a view in `views.py`, and a new payload field shows up in the human
    form only when its view shows it (fields views show every scalar).

## `pulsar orbit-tool` exits 0 when it answered

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/orbit/backend.py::main`

### Context

Orbit's exec protocol reads the outcome from the response envelope (`ok`); a non-zero exit
means the backend crashed and its output is discarded.

### Decision

`orbit-tool` exits 0 whenever it wrote a response, including `{ok: false, error}`.

### Consequences

- Orbit shows the caller the pulsar error code and message instead of a crash.
- Cost: a shell caller of `pulsar orbit-tool` must read `ok`, not the exit status.

## Renamed flags stay for one release

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/cli/commands/publish.py::_publish`, `src/pulsar/cli/commands/auth.py::_status`

### Context

`--confirm` is the one confirmation spelling; `publish` used `--yes`.
`auth status` became offline by default, which left `--offline` with nothing to do.

### Decision

`publish --yes` stays as an alias of `--confirm` and `auth status --offline` as a
no-op, each warning on stderr, for one release; then both become usage errors. `auth logout`
and `import-posted --confirm` are new requirements, not renames, and have no alias.

### Consequences

- Existing routines keep working and are told what to change.
- Cost: two spellings of confirmation for a release.

## A POST without an Origin is an MCP client, not a browser

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/mcp/server.py::loopback_security`, `tests/test_server.py::test_http_accepts_only_its_own_loopback_authority`

### Context

A loopback HTTP server usually refuses every state-changing request unless it carries a
loopback `Origin` matching the `Host`. MCP's streamable HTTP transport is a `POST` per message, and the clients that speak it
(Claude Code, Codex, SDK clients) are not browsers and send no `Origin`. Held to the letter,
`pulsar serve --transport http` could serve no client at all.

### Decision

A request with no `Origin` is accepted when its `Host` is one of the bound server's loopback
authorities (`127.0.0.1`, `localhost` or `::1` at the bound port, per `--host`). A request that
carries an `Origin` is accepted only when it is the `http` origin of one of those same
authorities; anything else, including `null`, `https` and another port, is 403. `localhost`
and `127.0.0.1` name the same bound socket, so an `Origin` of one with a `Host` of the other is
accepted rather than matched character for character. The server binds
loopback only (`--host` is a closed choice) and the `Host` allow-list is exact (421 otherwise).

### Consequences

- MCP clients work over HTTP; browsers, which always send `Origin` on a cross-origin `POST`,
  are held to the `Origin` check, and DNS rebinding fails the `Host` check.
- Cost: a local non-browser process can drive the server without an `Origin`, as it could over
  stdio. The server holds no credential it returns, and every write still passes the ledger,
  the policy and the secret scanner.

## No supply-chain gate beyond the lock and dependabot

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `Makefile` (`lint`: `uv lock --check`), `.github/dependabot.yml`, `uv.lock`

### Context

A supply-chain gate would deny yanked packages, open advisories and unknown sources, and
allow-list licenses. For pulsar that is an advisory audit and a license check of `uv.lock`. `make check` must also run offline, and an advisory audit
needs the network.

### Decision

For now the gate is: every dependency pinned with hashes in `uv.lock` from PyPI only,
`uv lock --check` in `make check` so the lock cannot drift from `pyproject.toml`, and
dependabot proposing updates and security fixes. A Python audit gate (advisories, yanked
releases, licenses, with dated exceptions) is follow-up work.

### Consequences

- A lock that drifted from `pyproject.toml` fails `make check`, and every install is the
  hashed, PyPI-sourced set in `uv.lock` (`uv sync --frozen`).
- Cost: a known-vulnerable or yanked pin is caught only when dependabot raises it, and licenses
  are not checked. Re-review when the audit gate lands.

## The launcher's sync is bounded only where timeout(1) runs

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `bin/pulsar` (the `uv sync` step), `tests/test_launcher.py`

### Context

The first plugin call after an install or upgrade runs `uv sync`, which fetches dependencies.
It should be bounded. The launcher is POSIX `sh`; stock macOS ships no `timeout(1)`, and
a watchdog written in `sh` would need its own process-group handling that the host already
does.

### Decision

Where `timeout` runs (it is probed, not trusted), the sync is `timeout -k 10 600`: TERM at
600 s, KILL 10 s later. Where it does not, the sync runs under the Orbit host's per-call
timeout alone, which ends the backend's process tree. The launcher waits for `uv` and
reports a failed or timed-out sync as `dependency_sync`, but does not sweep `uv`'s process
group after a clean exit: `sh` has no portable way to, and the host's per-call tree
kill reclaims anything left.

### Consequences

- Linux hosts, where pulsar runs today, are bounded by the launcher itself.
- Cost: on a Mac without coreutils a stalled sync holds the call until the host gives up.

## CLI output fields renamed before the first release

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/app/ops.py::validate_report`, `src/pulsar/app/ops.py::publish_report`, `tests/goldens/cli/`

### Context

Aligning the CLI with orbit's changed some of its JSON. `validate` and a `publish` preview
answered `{ok: true, accounts}`; they now answer `{valid, published, accounts, note}`. A
`publish` result entry for an account that failed before sending spread the error's `code`
and `message` across the entry; it now has a receipt's keys plus `error: {code, message,
retryable, detail}`. That is a breaking change; a renamed field is usually kept for a
release, with a warning.

### Decision

No alias period. pulsar has never been released (0.1.0, no tag), and nothing in the
constellation parses these commands' output. The Orbit plugin and the MCP server have their
own schemas and changed only by adding fields. The new shapes are pinned by `tests/goldens/cli/`, and
the next rename keeps the old field for a release.

### Consequences

- One shape per record from the first release on.
- Cost: a script written against the pre-release output breaks without a warning.

## Reconcile and first-use migration apply without `--confirm`

**Recorded:** 2026-09-26 · [ORB-13138]
**Code anchors:** `src/pulsar/app/ops.py::reconcile_report`, `src/pulsar/cli/commands/auth.py::_migrate_quietly`, `src/pulsar/app/runtime.py::Runtime.writer`

### Context

A bulk or cleanup command reports by default and applies only with `--confirm`.
`publish`, `auth logout`, `import-posted`, `migrate` and `auth migrate` do. Two effects do
not. `pulsar reconcile` settles every unknown row of an account from X's timeline, and the
timeline read is billed per post returned. The first write command or tool call on a phase 1
home also moves its credentials into the account layout.

### Decision

`reconcile` applies what it finds: a report-only run would pay for the same timeline read and
then a second one to apply it. It records only what the timeline proves (a row is marked
absent only with a complete listing, a grace period and a fingerprint) and never sends or
deletes. First-use migration stays automatic, so a routine keeps working across the upgrade.
It renames within the home, refuses to overwrite credentials, and is what `pulsar migrate
--confirm` would do. Reports never migrate.

### Consequences

- An operator runs one paid read to settle an account, and upgrades need no manual step.
- Cost: reconcile gives no preview, and a phase 1 home is changed by its first write command
  without being asked.

## Task References

- [ORB-12124] — built the MCP server.
- [ORB-13029] — built the Orbit plugin.
- [ORB-13114] — Orbit: structured plugin errors (ws_orbit).
- [ORB-13138] — aligned the surfaces with the constellation standards.
- [ORB-13248] — human output, grouped help and plain errors for the CLI.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
