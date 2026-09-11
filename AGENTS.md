# Agent guidance

- This repository is `pulsar`, the constellation's X write connector — an MCP
  server (Python, `uv`) exposing `whoami`, `create_post`, `upload_media`,
  `delete_post`. Reads are not its job.
- Credentials never cross the tool boundary. No tool may accept a token, key,
  or secret as an argument, and no tool may return one. Token storage stays
  encrypted under the pulsar home directory.
- Every live write goes through the local write log and the secret scanner.
  Do not add a bypass flag for either.
- Live X calls need a human-authorized token on the host; tests must never hit
  the network — use the fake transport in `tests/`.
- `agent-main` is the landing branch; commit directly, no PR gate. Daniel owns
  releases and any change to the X app registration.
- Run `make check` before handing off.
