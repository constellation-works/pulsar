# Agent guidance

- This repository is `pulsar`, the constellation's social connector (X today;
  Python, `uv`): an MCP server, the `pulsar` CLI and an Orbit plugin over one
  core and one ledger. It writes, and reads only what engagement needs (the
  account's mentions, its own posts' metrics); search and general timelines are
  not its job, and it never stores what it reads.
- Credentials never cross the tool boundary. No tool may accept a token, key,
  or secret as an argument, and no tool may return one. Token storage stays
  encrypted under the pulsar home directory.
- Every live write goes through the ledger (SQLite `ledger.sqlite3`, recorded
  before the network call; `writes.jsonl` is its export) and the secret
  scanner. Do not add a bypass flag for either.
- Approving is a human act. No tool on any surface records or revokes an
  approval, and a publish of an agent's draft never skips the approval check.
- Live X calls need a human-authorized token on the host; tests must never hit
  the network — use the fake transport in `tests/`.
- `agent-main` is the landing branch; commit directly, no PR gate. Daniel owns
  releases and any change to the X app registration.
- The installable Orbit plugin is `.orbit-plugin/`: its manifest, launcher,
  schemas, skills, definitions and conformance goldens live there. Root `src/`,
  `pyproject.toml` and `uv.lock` are canonical; `make plugin` refreshes their
  generated copies in the plugin root. A tool change updates its schema and
  goldens; `orbit plugin test .` runs them under the sandbox on an Orbit build
  with the dedicated plugin-root layout. No symlinks in the plugin tree.
- Design docs live in `docs/design/<feature>/` and follow
  `docs/design/CONVENTIONS.md`. A behaviour change updates its design doc (and
  the README only if the front door changed) in the same commit; keep the README
  minimal.
- Run `make check` (`uv lock --check`, ruff, basedpyright strict, pytest)
  before handing off.

## Conventions

- The tree is the architecture; [ARCHITECTURE.md](docs/design/ARCHITECTURE.md) has it and
  `tests/test_layers.py` enforces it. Front ends (`cli`, `mcp`, `orbit`) use `app` and
  `internal`, never `app/core`: each has its verbs in `app` (`ops`, `tools`, `plugin`).
  `core` never imports `app`; `internal` imports nothing above it.
- Inject dependencies: a class takes them in its constructor, a function as parameters.
  Only the entry point reads the process (environment, cwd, `$HOME`), and only `app/runtime.py`
  and `app/facade.py` build the implementations. Type an injected dependency by a
  `typing.Protocol` named for its role (`Ledger`, `CredentialStore`, `Channel`).
- A package with subpackages keeps its `__init__.py` to a docstring, so importing one module
  beneath it stays cheap. A package without subpackages may re-export its public names there.
