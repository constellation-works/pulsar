---
title: Surfaces — Vision
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Draft
feature: surfaces
doc_role: vision
type: design
summary: The phase 4 plugin tools (submit, publish, approve, dispatch), operator-only approval, and what Orbit must provide for them.
tags: [surfaces, orbit-plugin, approvals, routines]
paths: ["plugin.yaml", "src/pulsar/orbit_tool.py"]
related_features: [publishing]
related_artifacts: [ORB-13030, ORB-13114, ORB-13115]
---

# Surfaces — Vision

What the plugin grows into with phase 4 ([ORB-13030]). Nothing here is built.

## 1. Open Questions

1. **The full tool set.** Planned:

   | Tool | Kind | Scope | Purpose |
   |---|---|---|---|
   | `queue` | read_only | workspace | drafts and their schedule |
   | `submit` | mutating | workspace | create a draft from an inline plan or workspace source |
   | `publish` | mutating | workspace | an approved draft, or a plan under a standing policy |
   | `delete` | mutating | workspace | ledger-recorded delete |
   | `dispatch` | mutating | workspace | publish due approved drafts, reconcile unknown rows |
   | `approve`, `reject`, `revoke` | mutating | none | human-only, via `orbit pulsar …` |

   Orbit lets agents call a mutating plugin tool only when the task's `required_tools` or the
   activity allowlist names it; with the approval check, explicit intent becomes structural.
2. **Error detail on writes.** `budget_exceeded` needs `detail.retry_after` and
   `outcome_unknown` must reach callers as not retryable. The `valid: false` workaround does not
   fit writes; this waits on [ORB-13114] or needs a documented second shape.
3. **Caller identity.** Standing policies must match a host-attested task or routine
   ([ORB-13115]); until then publishing under a policy cannot be safely offered to agents.
4. **Definitions.** A `pulsar-dispatch` routine (every 5 minutes, a deterministic
   `plugin.tool_call`, no model) and a `pulsar-auth-health` auto-task, both seeded disabled.
5. **Who pins the plugin.** No committed `.orbit/plugins.yaml` pins pulsar until Daniel names
   the owning workspace (likely ws_marketing).
6. **Does the standalone MCP server stay?** It serves callers outside Orbit; whether any remain
   once the Mac is retired as a host is open.

## 2. Prior Work

### Orbit plugins

The graph plugin (orbit-graph) is the working reference for an exec-backend plugin with
panels and a skill. Orbit's plugin standard is `docs/design/plugins/1_scope.md` in the orbit
repository.

### Permission layers in agent harnesses

Claude Code and similar harnesses gate tools by name and MCP annotations; pulsar's annotations
are shaped for that.

## 3. What May Be Distinctive

Approval as a tool no agent can reach (`mcp_scope: none`), bound to a content digest the
publish step re-verifies, rather than a flag an agent could set.

## 4. References

**pulsar-internal**

- [Surfaces — Design](./2_design.md)
- [Publishing — Vision](../publishing/3_vision.md)

**External**

- orbit repository: `docs/design/plugins/1_scope.md` (the plugin standard).

## Task References

- [ORB-13030] — phase 4: lifecycle tools, definitions, migration.
- [ORB-13114] — Orbit: structured plugin errors (ws_orbit).
- [ORB-13115] — Orbit: host-attested task and run id (ws_orbit).

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
