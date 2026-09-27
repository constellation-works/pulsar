---
title: Surfaces — Overview
owner: claude
last_updated: 2026-09-27
last_validated: 2026-09-26
status: Accepted
feature: surfaces
doc_role: overview
type: design
summary: pulsar's three front ends over one core and one ledger — the standalone MCP server, the operator CLI and the Orbit plugin.
tags: [surfaces, mcp, cli, orbit-plugin]
paths: ["src/pulsar/**", "plugin.yaml", "bin/pulsar", "schemas/**", "skills/**", "tests/conformance/**"]
related_features: [publishing, accounts]
related_artifacts: [ORB-12124, ORB-13028, ORB-13029, ORB-13030]
---

# Surfaces — Overview

pulsar is reached three ways, all over the same core, the same home and the same ledger: a
standalone MCP server for agents outside Orbit, the `pulsar` CLI for the operator (auth and
operator verbs), and an Orbit plugin (`pulsar.*` tools, dashboard panels, a skill). A surface
translates requests and errors; it never decides policy and never touches a credential.

## 1. Motivation

- **Different callers, one set of rules.** A Claude session, an Orbit routine and a human at a
  shell must all hit the same budget, the same idempotency keys and the same secret scanner.
- **Orbit is where the constellation's agents live.** A plugin gives them the tools with
  Orbit's sandbox, grants, audit and dashboard, instead of a side-channel MCP registration per
  client.
- **Some actions are human-only.** Login, and (phase 4) approval, must be reachable by the
  operator and by no agent.

## 2. Core Concepts

- **Runtime.** A surface's handle on one home: settings, registry, ledger, publisher
  (the `Runtime` protocol in `app/interfaces.py`; `LocalRuntime` implements it).
- **Tool annotations.** MCP `readOnlyHint` / `destructiveHint` / `idempotentHint`, so a
  harness can gate by tool without parsing arguments.
- **Operator verbs.** `pulsar status | history | validate | publish | reconcile |
  import-posted` (`app/ops.py`).
- **Exec envelope.** Orbit's one-request-per-process protocol: JSON in on stdin, one JSON line
  out.
- **Caller.** A self-asserted audit label on writes; advisory, never authorization.

## 3. At a Glance

| Concern | File | Task |
|---------|------|------|
| MCP server: schemas, annotations, errors as results | [mcp/server.py](../../../src/pulsar/mcp/server.py) | [ORB-12124], [ORB-13028] |
| Agent tools behind it (claim, send, settle) | [app/tools.py](../../../src/pulsar/app/tools.py) | [ORB-12124], [ORB-13028] |
| CLI: auth, operator verbs, `serve`, `orbit-tool` | [cli/](../../../src/pulsar/cli/) | [ORB-13027], [ORB-13028], [ORB-13029] |
| Operator verbs | [app/ops.py](../../../src/pulsar/app/ops.py) | [ORB-13028] |
| Orbit exec backend | [orbit/backend.py](../../../src/pulsar/orbit/backend.py) | [ORB-13029] |
| Plugin manifest, launcher | [plugin.yaml](../../../plugin.yaml), [bin/pulsar](../../../bin/pulsar) | [ORB-13029] |
| Tool schemas, skill, goldens | [schemas/](../../../schemas/), [skills/publish/](../../../skills/publish/SKILL.md), [tests/conformance/](../../../tests/conformance/pulsar.yaml) | [ORB-13029] |

Error codes shared by all surfaces: [references/error-codes.md](./references/error-codes.md).

## Task References

- [ORB-12124] — built the MCP server and its first tools.
- [ORB-13027] — added `auth status --live` and the storage checks surfaced by the CLI.
- [ORB-13028] — moved every write onto the publisher; added `validate_plan` and the operator verbs.
- [ORB-13029] — added the Orbit plugin: backend, manifest, schemas, panels, skill, goldens.
- [ORB-13030] — phase 4: the lifecycle tools (submit, publish, approve, dispatch).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
