# Agent guidance

- This repository is `pulsar`, the constellation's social write connector (X
  today; Python, `uv`): an MCP server, the `pulsar` CLI and an Orbit plugin over
  one core and one ledger. Reads are not its job.
- Credentials never cross the tool boundary. No tool may accept a token, key,
  or secret as an argument, and no tool may return one. Token storage stays
  encrypted under the pulsar home directory.
- Every live write goes through the ledger (SQLite `ledger.sqlite3`, recorded
  before the network call; `writes.jsonl` is its export) and the secret
  scanner. Do not add a bypass flag for either.
- Live X calls need a human-authorized token on the host; tests must never hit
  the network — use the fake transport in `tests/`.
- `agent-main` is the landing branch; commit directly, no PR gate. Daniel owns
  releases and any change to the X app registration.
- The Orbit plugin is `plugin.yaml` + `bin/pulsar` + `schemas/` +
  `skills/publish/` + `tests/conformance/`. A tool change updates its schema
  and goldens; `orbit plugin test <clean export> --grant fs,network` runs them
  under the sandbox. No symlinks in the tree: the installer refuses them.
- Design docs live in `docs/design/<feature>/` and follow
  `docs/design/CONVENTIONS.md`. A behaviour change updates its design doc (and
  the README only if the front door changed) in the same commit; keep the README
  minimal.
- Run `make check` (`uv lock --check`, ruff, basedpyright strict, pytest)
  before handing off.

## Conventions

- **Inject dependencies, or move the module down.** A module gets what it
  depends on as an argument: a class takes it in its constructor, a function
  takes it as a parameter. It does not import or build it for itself. For
  example, `pulsar.main` builds the `App` and passes it to `pulsar.cli.run`.
  If a module is used by only one higher module, move it beneath that module
  instead: `cli/toolkit/` sits under `cli/` because only the CLI uses it.
- **Each package exposes what others use in its `__init__.py`.** Everything
  imported from outside a package is re-exported in its `__init__.py` and
  listed in `__all__`. Callers import the package, not its leaf files: for
  example, `from pulsar.cli import toolkit`, then `toolkit.notice`.
- **A package holds only leaf modules that belong to the group it is named
  for.** `commands/` holds commands. What the commands stand on is not a
  command, so it lives in `toolkit/`. A flat, generic package that holds
  everything, such as `core`, is the thing to avoid: split it into packages
  named for what they hold.
