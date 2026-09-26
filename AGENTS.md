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
- Layout, bottom up: `src/pulsar/core` (provider-neutral, no HTTP),
  `providers/<name>` (one channel adapter each), `app` (the `App` and the
  runtime every front end calls), the front ends `mcp.py`, `orbit_tool.py`
  (Orbit exec backend) and `cli/` (`toolkit/` < `commands/` < `main.py`), and
  the entry point `main.py`. Keep `core` free of provider and transport
  imports.
- **Dependencies point down, and the directory tree shows it.** A module
  imports only what sits beneath it: its own package's modules and
  subpackages (`from .x import ...`), or a lower layer through that layer's
  package root and only the names its `__all__` lists (`from pulsar.core
  import Ledger`). Never upward or across: no `from ..`, no reaching into
  another package's modules. When two modules need the same thing, move it
  beneath both; when something needs a value from above, the top passes it
  down (`main.py` builds the `App` and supplies it). `tests/test_layering.py`
  enforces it; `core/ledger` is the one known exception (it still reaches
  core's shared modules with `from ..`).
- **A package holds only what it is named for.** What its members are written
  against is not one of them: it goes in its own package, named for what it
  is, beneath them. `cli/commands/` holds commands only; the parser pieces,
  context, rendering, views and errors they use are `cli/toolkit/`, a lower
  member of `cli`, reached through its root. `tests/test_layering.py` checks
  that every module in `cli/commands/` declares a command.
- The Orbit plugin is `plugin.yaml` + `bin/pulsar` + `schemas/` +
  `skills/publish/` + `tests/conformance/`. A tool change updates its schema
  and goldens; `orbit plugin test <clean export> --grant fs,network` runs them
  under the sandbox. No symlinks in the tree: the installer refuses them.
- Design docs live in `docs/design/<feature>/` and follow
  `docs/design/CONVENTIONS.md`. A behaviour change updates its design doc (and
  the README only if the front door changed) in the same commit; keep the README
  minimal.
- Run `make check` (standards check, `uv lock --check`, ruff, basedpyright
  strict, pytest) before handing off.

## Verification and handoff (STD-04)

- A regression test is shown failing against the unfixed code (revert the fix
  or run the test on the base commit) before the fix counts; keep that run as
  evidence. Fix the defect; never loosen a guard, assertion or golden to pass.
- A red gate is reproduced on a clean checkout of the base commit before it is
  called pre-existing.
- Before starting, check the task's premise against the current head: it may
  have landed or moved.
- The handoff lists each command run as passed, failed or not run (with why);
  "tested" alone is not a result.
- Read back what you wrote to Orbit or git (task state, the pushed commit)
  before reporting it; after an uncertain write, query before retrying.
- Before anything destructive (reset, branch delete, file removal), record the
  commit or an inventory of what goes. Never stash, clean or discard edits you
  did not make.

<!-- constellation-standards:begin -->
<!-- Managed by the constellation's operations/scripts/sync-standards.sh; edits inside this block are overwritten. -->
## Constellation standards

This repository adopts these constellation standards, vendored read-only in `docs/standards/`:

- `STD-01@2` — [docs/standards/STD-01-cli-surface.md](docs/standards/STD-01-cli-surface.md)
- `STD-02@2` — [docs/standards/STD-02-rust-architecture-and-errors.md](docs/standards/STD-02-rust-architecture-and-errors.md)
- `STD-03@2` — [docs/standards/STD-03-concurrency-and-process-safety.md](docs/standards/STD-03-concurrency-and-process-safety.md)
- `STD-04@1` — [docs/standards/STD-04-testing-and-verification.md](docs/standards/STD-04-testing-and-verification.md)
- `STD-05@1` — [docs/standards/STD-05-security-boundaries.md](docs/standards/STD-05-security-boundaries.md)

Follow them; they are normative. To deviate from a rule, record a decision in `docs/design/<feature>/4_decisions.md` citing `STD-nn@<version> §Rn`; never edit `docs/standards/` (`sh docs/standards/check.sh` enforces this).
Reviewers check every change against the adopted standards and report violations as `STD-nn §Rn` with file:line evidence.
<!-- constellation-standards:end -->
