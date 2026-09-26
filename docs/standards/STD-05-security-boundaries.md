---
id: STD-05-security-boundaries
title: Security boundaries — authority, filesystem containment, child environments, redaction, local network surfaces, supply chain and consent
summary: Normative trust-boundary rules for constellation CLIs and small local services — authority from host-owned state, symlink-safe path containment, private local state, allowlisted child environments, value-based redaction at the durability boundary, loopback surfaces that check Host and Origin, no phoning home, digest-bound consent, pinned and verified supply chain, CSPRNG tokens; follow it in any tool that runs untrusted children, persists state, or listens on a socket.
status: active
tags: [standard, security, sandbox, secrets, redaction, supply-chain, network]
version: 1
created: 2026-09-26
updated: 2026-09-26
last_validated: 2026-09-26
related: [STD-01-cli-surface, STD-02-rust-architecture-and-errors, STD-03-concurrency-and-process-safety, STD-04-testing-and-verification]
---

# Security boundaries

These rules decide where a tool's trust boundaries sit and how it keeps them. A trust
boundary is where input from something the tool does not control reaches something it
protects: an agent subprocess calling back into the tool, a path a plugin names, a browser
page reaching a loopback socket, a dependency pulled at build time. Most rules below come
from Orbit security reviews that found a boundary which only looked like one. The rules are
written for a small CLI or a local service that is not Orbit. Orbit is the worked example,
not the audience. Orbit paths are relative to `codebases/orbit/`.

Three things this standard does not repeat:
- **Secrets in the repository.** Polaris CONVENTIONS §4
  ["Never commit secrets"](../CONVENTIONS.md#never-commit-secrets) is a standing policy for
  every agent. It covers docs, records, commit messages and filenames. The rules here cover
  what a running program does with secrets.
- **Fail closed.** When a rule below cannot be decided — the authority fact, the path, the
  digest or the owner cannot be verified — the tool refuses. That is STD-02 (fail closed on
  unverifiable security state), and every rule here inherits it.
- **Subprocess lifecycle.** Process groups, termination and reaping are STD-03 §R11–R16, and
  test-fixture environments are STD-03 §R20. The rules here cover what a child is *given*.

The keywords MUST, MUST NOT and SHOULD are normative. A rule marked *review-only* in
§Machine checks has no automated gate, so a reviewer enforces it.

## Rules

### Authority and identity

- **R1.** Authorization, identity and provenance MUST derive from state the host controls and
  the subject cannot rewrite: a host-minted session record, a descriptor the host opened, an
  identifier stamped at write time, file ownership. Environment variables, self-declared
  labels, process ancestry and request payloads are claims, not authority.
- **R2.** A caller's self-reported identity that is worth keeping MUST be stored in its own
  field, apart from the authenticated identity. It never replaces or widens the
  authenticated identity.
- **R3.** Whether a surface lists or hides an operation MUST NOT be used as a permission
  check. Permission is decided by one authorization function that every entry surface calls
  before the operation runs.
- **R4.** An escape hatch that raises a caller's authority MUST be explicit and named, and
  every use MUST be audited with its own provenance, so an override is never
  indistinguishable from an ordinary call.
- **R5.** A guard that the subject can bypass (for example, one enforced against a process
  running as the same OS user) MUST be documented as an accident guard, not a security
  boundary. No other control may be relaxed because that guard exists.

### Filesystem boundaries

- **R6.** Path containment MUST be decided on the physical path, checked against an
  allowlist of roots. Canonicalize a path that exists. For a path that does not exist yet,
  canonicalize the nearest existing ancestor and append the missing names. Never decide on
  the spelled or lexically normalized path alone.
- **R7.** The operation MUST act on the path that was checked. The check and the operation
  share one resolution. A write or create does not follow a symlink in its final component
  (`O_NOFOLLOW` or an equivalent `lstat` check), and it refuses when what it created does
  not resolve to the identity that was validated.
- **R8.** Local state files and directories MUST be created with owner-only modes (`0600`
  for files, `0700` for directories), set explicitly at creation and never left to the
  process umask.
- **R9.** State that grants authority or holds secrets MUST be validated when it is loaded:
  owned by the current user, not group- or world-writable, and not reached through a
  symlink. On failure, the tool refuses to use it, or repairs the modes and warns.

### Process and environment

- **R10.** A child process that is not fully trusted MUST get an environment composed from
  one explicit allowlist: a documented non-credential baseline, the names the operator
  configured, and the names the child's integration declares it needs. The child launches
  from a cleared environment. Never filter the parent environment with a denylist or with
  name or value heuristics.
- **R11.** Privilege-bearing variables in the tool's own namespace (operator overrides,
  claim tokens, callback credentials) MUST NOT reach an untrusted child, even when a
  caller-supplied pass list names them explicitly.
- **R12.** A secret MUST NOT be passed to a child in argv, and MUST NOT sit in an
  environment that children inherit without needing it. Hand a secret over through a private
  file, an inherited descriptor or stdin.

### Secrets and redaction

- **R13.** Redaction MUST run at the durability boundary. Every writer that persists free
  text — log sinks, audit blobs, stored artifacts, persisted error messages — passes its text
  through one redactor before the write. The set of covered writers is an explicit inventory
  that a test checks.
- **R14.** Redaction MUST match secret values, not vocabulary. It masks the live values of
  secret-bearing variables and high-confidence credential shapes (bearer headers, provider
  key prefixes). It never masks an ordinary word because a secret-named variable happens to
  hold it, and never masks part of an ordinary identifier.
- **R15.** A tool that uses the operator's credentials MUST read them where the operator
  keeps them (the provider's own credential store, or a variable the operator names for
  pass-through). It MUST NOT copy them into its own state.

### Network surfaces

- **R16.** A loopback bind MUST NOT be treated as authentication. A local HTTP server
  validates the `Host` header against an approved loopback authority on every route,
  including health and diagnostics, and refuses a missing or unparsable `Host`.
- **R17.** A local HTTP server MUST refuse every state-changing request (`POST`, `PUT`,
  `PATCH`, `DELETE`), and every request that carries an `Origin` header, unless `Origin` is
  a loopback `http` origin that matches the validated `Host`.
- **R18.** A listener that speaks a protocol other than HTTP MUST reject any stream that
  does not open with that protocol's framing before it dispatches anything. That stops a
  browser's HTTP request from being parsed as protocol input.
- **R19.** A network listener SHOULD bind loopback by default and refuse a wider bind
  unless it is requested explicitly. Remote access SHOULD reuse an authenticated transport
  the operator already has (SSH forwarding) rather than add new credentials or a routable
  listener.
- **R20.** A tool MUST NOT make a network request that the operator did not initiate or
  configure. That rules out an update check on startup, usage telemetry and crash reporting
  unless the operator turned them on.

### Supply chain and consent

- **R21.** A consent or grant MUST bind to a digest of the content that was approved, not to
  a name. When the content on disk no longer matches the recorded digest, the grant does
  not apply until the operator approves again.
- **R22.** A registry that accepts names from a less-trusted source (plugins, extensions,
  user definitions) MUST refuse a name that collides with a built-in or with another
  source's entry. It never overwrites or shadows it silently.
- **R23.** Build, CI and install inputs MUST be pinned to immutable identifiers and verified
  when they are fetched: a committed lockfile, CI actions pinned by full commit SHA, and
  downloaded tools and release artifacts checked against a recorded checksum or signature.
- **R24.** Every third-party dependency, including code vendored or copied into the
  repository, MUST have a machine-readable record that advisory monitoring reads.
- **R25.** Every token, nonce or session identifier with security meaning MUST be drawn from
  the operating system's cryptographically secure random source.

## Why

Evidence below cites Orbit tasks (`ORB-`) and frictions (`F`), both in the ws_orbit store.
`D:<area>:<n>` means line *n* of `docs/design/<area>/4_decisions.md` in the Orbit repo. The
candidates were extracted and accepted in
`projects/standards/tacit-extraction-2026-09-26.md` (disposition recorded on ORB-13088);
each rule names the candidate it came from.

**R1.** *Candidate T14.* A value the subject can rewrite cannot tell the host who the subject
is. Orbit had a run of these:
- ORB-12775: the plugin tool allowlist was enforced through `ORBIT_ALLOWED_TOOLS`, which the
  sandboxed child could simply unset.
- ORB-12789: the first fix moved the gate to `ORBIT_PLUGIN`, another variable the child
  controls, and the gate was skipped on the MCP entry point.
- ORB-12798: the fallback identity used pid, parent pid and process group. A descendant that
  called `setsid` matched nothing the host had recorded, and its identity vanished.
- ORB-12768 and ORB-11607: privilege-bearing variables reached children that could then
  present them back.

The fix that held gives the backend a descriptor on a session record the host wrote, in a
directory the plugin cannot write. A child can close that descriptor, but it cannot forge
one. D:federated-mcp:203 states the principle: "an authorization statement that the actor can
rewrite is documentation, not a boundary." D:distributed-drain:166 makes the same point
about payloads: "a payload that names its own host can name any host." D:policy-sandbox:264
adds that a sandbox binary found earlier on `PATH` must be ignored, so the boundary does not
depend on inherited environment ordering.

**R2.** *Candidate T14.* Clients legitimately describe themselves, and that is useful in an
audit trail. D:auditability:580 records the decision to "add a second field rather than
relaxing the boundary". The authenticated `role` keeps its meaning, and a nullable
`self_reported_actor` column holds whatever the client claims. Merging the two would let any
client write its preferred identity into the trusted field.

**R3.** *Candidate S7.* D:operations-as-data:154: hiding destructive tools from the MCP
listing was "advertisement, not enforcement". CLI subcommands still reached the same tools
through an admin bypass. D:operations-as-data:211 makes the split explicit: "Placement is not
permission." Where an operation is listed is an audience decision. One governed-operations
registry, resolved at one chokepoint per surface, is the only authorization statement.

**R4.** *Candidate S7.* D:operations-as-data:170: when authority is ambiguous, the resolution
fails closed "with one explicit escape hatch". Orbit's `ORBIT_OPERATOR` override is
deliberately easy to set. It exists to make a deliberate act look deliberate, not to stop a
determined caller. Every use is audited with its own provenance, so the trail shows an
override and not an ordinary operator session.

**R5.** *Candidate S7.* D:operations-as-data:158: an authorization layer between processes
of the same OS user is "an **accident guard, not a security boundary**". Any agent on the box
can bypass it with `git`, `rm` or a direct write. Adding a password or token to it "buys no
protection and invites relaxing the surrounding rails on the strength of a boundary that does
not exist." D:federated-mcp:203 gives the same reason for the dashboard. Labelling the guard
honestly keeps reviewers from pricing it as a boundary.

**R6.** *Candidate S1.* A check on the spelled path answers a question about names while the
kernel acts on inodes:
- ORB-12799: a granted write root whose missing tail sat below a symlink could not be
  canonicalized. The check fell back to a name-only reading and bypassed the protected-root
  guard.
- ORB-12788: a write grant could name any child of the global root, reopening directories
  other guards relied on being unwritable. The allowlist, not the grant, decides.
- ORB-11506: sandbox rules had to deny a rewrite of `.git` reached through leaf paths.
- F2026-09-098: an alert on a credential-file read stayed open because the read still took a
  caller-influenced path despite earlier canonicalization fixes.

Orbit's `SECURITY.md` states the resulting policy: canonicalize the target, or the nearest
existing ancestor for a new file, and deny "a symlink from an allowed tree into a denied
one."

**R7.** *Candidate S1.* A correct check does not help when the operation resolves the path
again, differently:
- ORB-12971: rule injection wrote through agent-guide symlinks to files outside the
  workspace.
- ORB-12800: plugin removal followed the hostile `install_path` that the loader had just
  refused.
- ORB-12799: the fix makes validation and enforcement share one resolution
  (`physical_with_missing_tail`). Creating the root refuses when what was created does not
  resolve to the validated identity.

`SECURITY.md` also records the gap no path check closes: the time between check and use. A
directory an attacker can change concurrently is untrusted.

**R8.** *Candidate P5.* ORB-11032: Orbit created its SQLite stores and state directories
with plain `create_dir_all` and `Connection::open`, so their modes came from the process
umask. The reviewed live install had a `775` state root and `644` databases and sidecars,
readable by every local user, while the main database held bearer claim tokens. The fix
creates files `0600` and directories `0700` explicitly. It hardens the database before
SQLite can create its write-ahead-log sidecars, because sidecars created under a loose umask
leak the same data.

**R9.** *Candidate P5.* ORB-12450: `mcp-callers.toml`, the file that states who may call a
machine and with which capabilities, was seeded with plain `std::fs::write` under the ambient
umask and loaded with no owner or mode check. Anyone else who could write it could grant
themselves operator capability. The same codebase already refused a companion override that
was not owned by the effective user or was group- or world-writable, so the check existed and
was simply not applied to the file that mattered most. A file that grants authority is
an authority input under R1, so its integrity is checked when it is read, not only when it is
written. The Orbit module that fixed ORB-12450 has since been folded into another design.
Orbit's current load path hardens modes and refuses symlinks, and `orbit doctor` reports
group- or world-writable state directories.

**R10.** *Candidate S2.* A denylist admits every name nobody thought to forbid. A benignly
named credential such as `DATABASE_URL` or an internal service URL reaches the child, even
though the operator turned inheritance off. Orbit's `child_env` module notes that
credential-name and value-shape heuristics "cannot classify names an operator's environment
actually uses, and treating them as a gate is what let the bypass exist." ORB-11031 brought
`proc.spawn` under the same allowlist as agent subprocesses. `docs/DATA_HANDLING.md` adds that
Orbit's OS sandbox is a filesystem boundary, not a network boundary, so the environment
allowlist is the main control on exfiltration. Orbit's `docs/POSITIONING.md` lists
sandboxed-by-default execution as a non-negotiable.
*Python:* `subprocess.run(argv, env=composed_env)`, where `composed_env` is built from named
keys, never `os.environ.copy()` with deletions.

**R11.** *Candidate S2.* ORB-11607: an `ORBIT_*` prefix wildcard admitted Orbit's own
privilege-bearing variables into agent subprocesses. ORB-12768: a plugin manifest's
`env_pass` copied any parent variable by name, including `ORBIT_OPERATOR` and
`ORBIT_WORKSPACE_CLAIM_TOKEN`. A pass list can come from an untrusted source, such as a plugin
manifest, so the exclusion holds "regardless of who is asking."

**R12.** *Candidate S2.* On Linux and macOS, another process of the same user can read a
process's argv, and often its environment. ORB-11134 moved SSH MCP acceptance bearers out of
same-UID process metadata. ORB-11184 found the capability still travelled in an environment
variable: the process sealed itself (`PR_SET_DUMPABLE=0`) only once `main` ran, and a sibling
process polling `/proc/*/environ` recovered the capability in the window before that. A secret in an inherited environment also reaches every grandchild, which is
the R10 problem again.

**R13.** *Candidate S3.* D:auditability:84 decides to redact secrets "before durable blob or
error-message persistence." Redacting at display time leaves the secret on disk. ORB-10958:
the artifact redaction allowlist missed `session_log.append` and other writers of persisted
free text, so those writers stored unredacted text. Orbit now keys its artifact redaction by
action. An explicit inventory, checked by a test, is what makes a missing writer visible.
`docs/DATA_HANDLING.md` is clear about the limits: "Redaction is a safety net, not the
boundary — the allowlist is the boundary." R10 to R12 keep secrets out, and this rule catches
what slips through.

**R14.** *Candidate S3.* F2026-08-081: a variable with a secret-bearing name held an ordinary
English word. Value matching then replaced every plain-English use of that word in a stored
task summary with `[REDACTED_ENV]`, corrupting the record. ORB-12084: an unanchored provider-key
pattern matched inside an ordinary hyphenated identifier and truncated it mid-word in a
stored task. Over-redaction destroys the audit trail that
redaction is meant to protect, and it teaches operators to turn redaction off. Orbit
therefore skips values shorter than four characters and a small set of ordinary words, and it
anchors credential patterns so they do not match inside identifiers.

**R15.** *Source: Orbit positioning, not a tacit candidate.* `docs/POSITIONING.md` lists
"Bring-your-own-credentials" as a non-negotiable: "API keys belong to the operator; Orbit is
pass-through." `docs/DATA_HANDLING.md`: "Orbit never stores a provider key in its own state."
A copy of a credential in the tool's state is one more place to leak from, and one more place
a rotation misses.

**R16.** *Candidate S4.* A browser on the same machine can reach a loopback socket. DNS
rebinding makes an attacker's hostname resolve to `127.0.0.1`, so the browser treats the
response as same-origin:
- ORB-12506: the dashboard never validated `Host`, so any website could read every API `GET`.
- ORB-12531: the first fix missed `/healthz?detailed=true`, which still leaked workspace
  names and host paths.

Browsers omit `Origin` on same-origin `GET`, so only a `Host` check stops a read.

**R17.** *Candidate S4.* Unsafe methods need an `Origin` check as well, because the classic
cross-site request forgery sends a cross-origin `POST` with the right `Host`. ORB-11613 fixed
Orbit's check to compare `Origin` against the request `Host`, and to accept `[::1]`, rather
than a prefix match. Both headers are client-supplied, and `curl` can set them to anything.
This is browser mitigation, not authentication. R16 and R17 together stop a web page, and the
loopback bind stops everything off the machine.

**R18.** *Candidate S4.* ORB-12929 and F2026-09-214: `orbit mcp listen` handed accepted
sockets straight to the protocol library. The library skipped lines that were not valid JSON,
so it skipped an HTTP request line and headers, then executed the JSON-RPC lines in the
`POST` body. Any web page could write tasks while the listener ran. The fix peeks at the
first byte and closes the connection unless it opens a JSON object.

**R19.** *Candidate S4.* D:remote-access:41: "Refuse every non-loopback Web bind" and reach a
remote dashboard through SSH, rather than adding dashboard credentials or a routable
listener. An SSH login already authenticates the operator. A new password system is new
attack surface that a small tool has no staff to maintain. This is a SHOULD because some
services exist to serve a network, and those record a deviation.

**R20.** *Source: Orbit positioning, not a tacit candidate.* `docs/POSITIONING.md`: "Orbit
must never phone home." `docs/DATA_HANDLING.md`: "no update check on startup, no crash
reporter, no usage analytics," and "no network call you cannot trace to a command you ran or
a crew you configured." An operator can only reason about data leaving the machine when every
request traces to something they did.

**R21.** *Candidate S5.* ORB-12767: the plugin loader never verified `manifest_digest`.
Grants recorded by name applied to whatever `plugin.yaml` was on disk, and audit rows
attested a digest that did not run. ORB-12803: plugin sync installed an unverified source
identity and reported a mismatched pin as satisfied. Consent given to one set of bytes has to
stop applying when the bytes change. Orbit now registers a plugin with a rewritten manifest
as inactive, and its diagnostic names both digests.

**R22.** *Candidate S5.* A name is a key only if it is unique:
- ORB-12804: colliding plugin definition names overwrote another plugin's seeded schedules.
- ORB-12787: a plugin's skill directory was linked into the provider's skills directory
  without a namespace, so a plugin with no grants replaced Orbit's own skill for every
  workspace on the host.
- ORB-12791: plugin tool names were validated against the manifest's `first_party` claim but
  registered from the store row, so a disagreeing row registered a plugin tool over a
  built-in.

**R23.** *Candidate S6.* ORB-11515: npm smoke CI ran an MCP publisher that was neither pinned
nor verified. ORB-12452: the website checks gate pinned `setup-node` to a commit that did not
exist, so every run failed at job setup and the gate enforced nothing. A pin has to be
verified to resolve, and a gate that cannot start is not a gate. Orbit pins CI actions by
SHA, checks downloaded CI tools against recorded SHA-256 sums, and verifies release
artifacts against a signed checksum manifest.

**R24.** *Candidate S6.* ORB-12684: vendored copies of DOMPurify and marked in the dashboard
had no dependency record, so no advisory service could see them. A dependency that nothing
records is one a published advisory never reaches. Orbit added a package manifest that
Dependabot watches, plus a vendor manifest whose digests CI checks against the served files.

**R25.** *Candidate S6.* ORB-11065: the SSH MCP acceptance token was the random part of a
temporary-file name, drawn from a non-cryptographic generator with a predictable seed. That
token was the only thing between an ordinary remote command and a forged MCP identity.
Temporary-name, hashing and simulation generators are built to be fast, not unguessable. *Rust:* `getrandom::fill`, or `rand::rngs::OsRng`. *Python:* the `secrets` module,
never `random`.

## Exemplars

Paths are relative to `codebases/orbit/`.

- R1 — `crates/orbit-tools/src/plugin/callback/mod.rs` (module doc: identity rides on a
  host-written descriptor); `crates/orbit-tools/src/plugin/callback/resolution.rs#credential_from_descriptor`
  (the descriptor must resolve to the same inode as the host's named record);
  `docs/design/federated-mcp/4_decisions.md`.
- R2 — `docs/design/auditability/specs/actor-identity.md`;
  `crates/orbit-store/src/driver/sqlite/audit_event_store/tests/self_reported_actor.rs`.
- R3 — `crates/orbit-common/src/governance/authorization.rs` (module doc, "Placement is not
  permission"; `GOVERNED_OPERATIONS` and `authorize`).
- R4 — `crates/orbit-common/src/governance/authorization.rs#OPERATOR_OVERRIDE_ENV`
  (audited as `CallerProvenance::OperatorOverride`).
- R5 — `crates/orbit-common/src/governance/authorization.rs` (module doc, "What this is, and
  what it is not"); `docs/design/operations-as-data/4_decisions.md`.
- R6 — `crates/orbit-exec/src/path_identity.rs#physical_with_missing_tail`;
  `crates/orbit-policy/src/engine.rs` (nearest-existing-ancestor canonicalization);
  `SECURITY.md#Sandbox model and known limits` ("Symlinks");
  `docs/design/policy-sandbox/specs/fs-profile-resolution.md`.
- R7 — `crates/orbit-exec/src/path_identity.rs#create_write_root`;
  `crates/orbit-common/src/fs/io.rs#open_read_only_no_follow`;
  `crates/orbit-exec/src/linux_sandbox/write_grants.rs#inspect_host_owned_anchor` (refuses a
  symlinked anchor).
- R8 — `crates/orbit-common/src/fs/io.rs#create_private_dir_all` and `#create_new_private_file`
  (`PRIVATE_FILE_MODE`, `PRIVATE_DIR_MODE`); `crates/orbit-common/src/storage/sqlite.rs#open_private`.
- R9 — `crates/orbit-common/src/storage/sqlite.rs#open_private` (repairs existing files to
  owner-only, `SQLITE_OPEN_NOFOLLOW`) and `#validated_sqlite_path`;
  `crates/orbit-cli/src/command/doctor.rs` (group- or world-writable state directory check).
- R10 — `crates/orbit-common/src/security/child_env.rs#allowlisted_child_env_from`
  (`AGENT_SUBPROCESS_BASELINE_VARS`); `docs/DATA_HANDLING.md#Credentials and secrets`.
- R11 — `crates/orbit-common/src/security/child_env.rs#is_privilege_bearing_orbit_name`.
- R12 — `crates/orbit-tools/src/plugin/callback/session.rs#PLUGIN_CALLBACK_FD` (the credential
  travels as an inherited descriptor, not argv or env).
- R13 — `crates/orbit-core/src/adapter/tool_host/artifact_redaction.rs#policy_for_action` and
  `#is_covered_mutating_action`; `docs/design/auditability/specs/artifact-redaction.md`.
- R14 — `crates/orbit-common/src/security/redaction.rs#redact_sensitive_env_text` and
  `#is_redactable_value`.
- R15 — `docs/POSITIONING.md#Non-negotiables`; `docs/DATA_HANDLING.md#Credentials and secrets`.
- R16 — `crates/orbit-web/src/api/origin.rs#require_localhost_origin` (gate 1, `Host`).
- R17 — `crates/orbit-web/src/api/origin.rs#require_localhost_origin` (gate 2, `Origin`) and
  `#localhost_origin_matches_authority`.
- R18 — `crates/orbit-mcp/src/listener.rs#serve_connection` (first-byte peek) and the module doc.
- R19 — `crates/orbit-mcp/src/listener.rs#ensure_bind_allowed` (`ListenerExposure`);
  `docs/design/remote-access/4_decisions.md`.
- R20 — `docs/DATA_HANDLING.md#What leaves the machine`; `docs/POSITIONING.md#What Orbit is NOT for`.
- R21 — `crates/orbit-core/src/runtime/plugin/host.rs#digest_mismatch_diagnostic`;
  `crates/orbit-tools/src/plugin/loader/load.rs` (`manifest_digest` computed from the bytes
  read); `docs/design/plugins/1_scope.md`.
- R22 — `crates/orbit-core/src/runtime/plugin/host.rs` (module doc: a colliding namespace is
  registered inactive); `crates/orbit-core/src/runtime/plugin/discovery.rs#host_plugin_registry`.
- R23 — `.github/workflows/ci.yml` (actions pinned by SHA; `CARGO_DENY_LINUX_SHA256` checked
  with `sha256sum -c`); `deny.toml#[sources]`; `crates/orbit-common/src/security/release.rs#verify_checksum_signature`.
- R24 — `.github/dependabot.yml`; `crates/orbit-web/assets/dashboard/vendor/VENDOR.md`,
  `crates/orbit-web/assets/dashboard/vendor/vendor-manifest.json` and
  `scripts/check-dashboard-vendor.py`; `deny.toml#[advisories]`.
- R25 — `crates/orbit-tools/src/plugin/callback/storage.rs#random_token` (`getrandom::fill`).

## Machine checks

Each gate below is real and can be adopted. "Orbit:" says what Orbit runs today.

| Rule | Gate |
|---|---|
| R1 | review-only. Add a test in which the child unsets or forges every environment variable and label, and assert it is refused or treated as an ordinary caller (Orbit: `crates/orbit-core/src/adapter/command/dispatch/tests/callback.rs`, `crates/orbit-cli/src/tests/plugin_callback_surface.rs`). |
| R2 | Test: a caller claims an identity, and the authenticated field is unchanged (Orbit: `audit_event_store/tests/self_reported_actor.rs`). |
| R3 | Test: call a governed operation from every surface, including an unlisted one, and assert the same decision (Orbit: `crates/orbit-common/src/governance/tests/authorization.rs`, `crates/orbit-core/src/runtime/tests/authorization.rs`). |
| R4 | Test: using the override writes an audit row with distinct provenance. Otherwise review-only. |
| R5 | review-only |
| R6 | Test: symlink fixtures, including a symlinked ancestor with a missing tail and a dangling link, must be refused (Orbit: `crates/orbit-exec/src/tests/path_identity.rs`). |
| R7 | Same symlink fixtures, asserting nothing is written outside the root. Otherwise review-only. |
| R8 | Test: create state under a permissive umask (`0002`) and assert `0600`/`0700` (Orbit: `crates/orbit-common/src/storage/tests/sqlite.rs`). |
| R9 | Test: a symlinked or group-writable state file is refused or repaired (Orbit: `private_open_rejects_symlink_database`, `private_open_repairs_existing_database_and_sidecars`). Runtime: a `doctor` check for group- or world-writable state (Orbit: `orbit doctor`). Checking the owning UID is review-only. |
| R10 | Test: build the child environment from a stated parent snapshot and assert benignly named credentials are absent (Orbit: `crates/orbit-common/src/security/tests/child_env.rs`). |
| R11 | Same test, with the privilege-bearing names listed in the pass list. |
| R12 | review-only. A secret scanner does not see runtime argv. |
| R13 | Rust: an exhaustive `match` over every persisting action with no `_` arm, so a new writer fails to compile until it is classified (Orbit: `policy_for_action`). Test: each covered family redacts (Orbit: `crates/orbit-core/src/adapter/tool_host/tests/artifact_redaction.rs`). Other languages: an inventory test that enumerates writers. |
| R14 | Test: ordinary words and hyphenated identifiers survive redaction (Orbit: `ordinary_words_are_not_redactable_env_values` in `crates/orbit-common/src/security/tests/redaction.rs`). |
| R15 | review-only. Optional: a secret scanner such as `gitleaks` over the tool's state directory in a test run. It also backs CONVENTIONS "Never commit secrets" in CI or pre-commit. Orbit: none configured. |
| R16 | Test: a forged `Host` on every route, health included, is refused (Orbit: `crates/orbit-web/src/api/tests/origin.rs`, `healthz_require_localhost_origin_rejects_forged_host_detailed`). |
| R17 | Test: cross-origin `POST` and prefix-matching origins are refused (Orbit: `require_localhost_origin_rejects_prefix_match` and siblings). |
| R18 | Test: send a browser-style HTTP `POST` whose body holds valid protocol lines, and assert nothing is dispatched (Orbit: `loopback_listener_closes_http_before_dispatching_post_body` in `crates/orbit-mcp/tests/mcp_wire_roundtrip.rs`). |
| R19 | Test: a non-loopback bind is refused without an explicit opt-in (Orbit: `crates/orbit-mcp/src/tests/listener.rs`). |
| R20 | review-only. Optional: run the CLI test suite in a network namespace with no route (`unshare -rn`), so an unexpected request fails. Orbit: not configured. |
| R21 | Test: rewrite an approved manifest and assert the grant no longer applies (Orbit: `a_rewritten_manifest_is_registered_inactive_naming_both_digests` in `crates/orbit-core/src/runtime/plugin/tests/host.rs`). |
| R22 | Test: register a colliding name and assert refusal, not replacement. Otherwise review-only. |
| R23 | `cargo build --locked` (or `npm ci`, `pip install --require-hashes`); `cargo deny check sources` with `unknown-registry = "deny"` and `unknown-git = "deny"`; `sha256sum -c` on every downloaded tool. Pinning CI actions by SHA is review-only unless a linter enforces it (optional: `zizmor`). Orbit: all of these except the linter. |
| R24 | `cargo deny check advisories` in CI (Orbit: `make audit`, `scripts/ci-guardrails.sh`); Dependabot or an equivalent for each ecosystem; a digest check for vendored files (Orbit: `scripts/check-dashboard-vendor.py` in `make ci-fast`). |
| R25 | review-only. Rust: `clippy::disallowed_methods` in `clippy.toml` can ban non-cryptographic generators in token code. Orbit: not configured. |

## Applies to

- `universal`: R1 to R7, R10 to R14, R20 to R25. Any tool that runs children, touches paths
  from another party, persists text, or has dependencies.
- `cli`: every `universal` rule. A single-binary CLI with no plugins meets R3 to R5, R21 and
  R22 trivially and says so.
- `stateful`: R8, R9, R13 and R15. Any tool that keeps local state across runs.
- `service`: R16 to R19. Any tool that listens on a socket, even on loopback.
- `rust`: the Rust gates in §Machine checks.

## Deviations

An adopting repo that departs from a rule records a decision in its own design docs, citing
`STD-05@1 §Rn` and the reason. It never edits its vendored copy of this standard.

Some deviations are expected, and the decision still needs to be recorded:
- A service meant to be reached over a network departs from `STD-05@1 §R19`. It then needs
  real authentication, and R16 and R17 still apply to any browser-facing route.
- An operator-enabled update check or telemetry is not a deviation from R20, because the
  operator configured it. A default-on check is, and cites `STD-05@1 §R20`.
- A platform without file modes (Windows) meets R8 and R9 with owner-only ACLs.

## Changelog

- **v1** (2026-09-26): first publication. R1 to R25 from tacit-extraction candidates T14,
  S1 to S7 and P5 (ORB-13088 disposition), plus R15 and R20 from Orbit's positioning
  non-negotiables. ORB-13129.
