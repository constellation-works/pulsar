---
title: Surfaces — Decisions
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
feature: surfaces
doc_role: decisions
type: design
summary: Exec backend over mcp, one settings source, plan errors as results, workspace-confined plugin media, and the launcher's shape.
tags: [surfaces, orbit-plugin, mcp]
paths: ["plugin.yaml", "bin/pulsar", "src/pulsar/surfaces/orbit_tool.py", "src/pulsar/surfaces/mcp.py"]
related_features: [publishing, accounts]
related_artifacts: [ORB-12124, ORB-13029]
---

# Surfaces — Decisions

Non-obvious decisions about pulsar's front ends. See
[CONVENTIONS.md §4](../CONVENTIONS.md#4-decisions) for the admission rule.

## Orbit runs pulsar as an exec backend, one process per call

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `plugin.yaml` (`backend.type: exec`), `src/pulsar/surfaces/orbit_tool.py::main`

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
**Code anchors:** `src/pulsar/surfaces/orbit_tool.py::_parse`, `schemas/config.json`

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
**Code anchors:** `src/pulsar/surfaces/orbit_tool.py::validate`

### Context

An agent drafting a post needs the error code and `detail` (`detail.at`, the weighted length)
to fix it. Orbit 0.24 passes only `code` and `message` of a plugin error on ([ORB-13114]).

### Decision

`pulsar.validate` returns `{valid: false, error: {code, message, retryable, detail}}` for a
plan that fails validation; bad tool input (no plan, unreadable source) stays a tool error.

### Consequences

- Drafting agents get the full detail through Orbit today.
- Cost: two error shapes on one surface; revisit when [ORB-13114] lands.

## Plugin calls confine media to the workspace

**Recorded:** 2026-09-26 · [ORB-13029]
**Code anchors:** `src/pulsar/surfaces/orbit_tool.py::_settings`, `src/pulsar/surfaces/orbit_tool.py::_source_path`

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

## Task References

- [ORB-12124] — built the MCP server.
- [ORB-13029] — built the Orbit plugin.
- [ORB-13114] — Orbit: structured plugin errors (ws_orbit).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
