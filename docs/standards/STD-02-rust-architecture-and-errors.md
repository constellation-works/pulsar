---
id: STD-02-rust-architecture-and-errors
title: Rust architecture and errors — layering, visibility, typed errors, failure semantics, logging, and test layout for constellation Rust projects
summary: Normative structure and failure rules for any constellation Rust repo (workspace or single-crate CLI) — dependency direction, crate splits, visibility, one definition per rule, typed thiserror errors translated at the boundary, errors that name verified causes, exhaustive matches, load-time config validation, unknown-never-reads-as-success, fail-closed vs fail-open, batch isolation, preconditions before side effects, lossless persisted shapes, tracing, dead code, test layout — plus the copyable lint, dependency-direction and cargo-deny gates. Read before creating or restructuring a Rust project or designing how it fails.
status: active
tags: [standard, rust, architecture, errors, failure-semantics, testing, lints]
version: 2
created: 2026-09-26
updated: 2026-09-26
last_validated: 2026-09-26
related: [STD-01-cli-surface, STD-03-concurrency-and-process-safety, STD-04-testing-and-verification, STD-05-security-boundaries]
---

# Rust architecture and errors

How a constellation Rust project is layered, what it exposes, how it fails, and where its
tests live. Orbit (`codebases/orbit`) is the worked example; the rules are written for a
repo that is not Orbit, including a small one. Several rules (R24–R34, and parts of R10 and
R16) are language-neutral principles about failure; they are stated portably, with the Rust
specifics as notes.

Neighbouring standards own the rest, and their rules are not repeated here:
- Concurrency, async, locking, channels, durable writes and subprocesses: STD-03
  (`STD-03-concurrency-and-process-safety`).
- The CLI's user-facing surface (flags, output streams, exit codes, how an error is
  presented, never silently dropping caller input): STD-01 (`STD-01-cli-surface`).
- Test strategy and verification beyond the layout rules R19–R21: STD-04
  (`STD-04-testing-and-verification`).
- Trust boundaries, authority and secrets: STD-05 (`STD-05-security-boundaries`).

## Rules

**Scaling.** Every rule applies to a multi-crate workspace and to a single-crate CLI. Where a
rule says *layer* or *unit*, read **crate** in a workspace and **top-level module** (for
example `src/domain/`, `src/app/`, `src/cli/`) in a single crate. A single crate expresses
dependency direction as module-import rules instead of `Cargo.toml` edges; the rules are the
same.

### Layering and structure

- **R1.** *Declared layer order.* Code in a lower layer MUST NOT import from a higher one,
  against an ordered layer model written in the project's architecture doc — foundation
  (contract types, shared mechanisms) → domain → composition → surfaces.
- **R2.** *Domain, composition, surfaces are separate.* Domain units own their data and
  logic; exactly one composition unit joins configuration, storage construction and domain
  services; surfaces (CLI parsing, HTTP/MCP handlers, rendering) MUST only parse input, call
  composition, and render the result.
- **R3.** *Resolved inputs flow down.* Domain and runtime code MUST receive resolved
  configuration and filesystem roots as values from composition, never load config or
  discover the cwd or `$HOME` itself.
- **R4.** *Mechanisms never depend on features.* Kernel units that expose reusable
  mechanisms (process, filesystem, codec, sandbox, observability helpers) MUST NOT depend on
  any domain, composition or surface unit.
- **R5.** *Contract types are I/O-free.* The foundation unit holding shared contract types
  (serde DTOs, IDs, pure constructors, narrow errors) MUST perform no I/O — no `std::fs`,
  `std::net`, `std::process`, database or HTTP — and depend on no crate that does.
- **R6.** *Direction is machine-checked.* Dependency direction MUST be enforced by a CI
  check, and a new edge changes that check and the architecture doc in the same change.
- **R7.** *Crates follow build boundaries, not taxonomy.* A new crate MUST be justified by a
  build or dependency need — compile-graph isolation, an independently consumed artifact, or
  a dependency edge that must be enforceable — not by conceptual category. A new CLI SHOULD
  start as two crates, because the domain/surface edge is exactly such an enforceable edge:
  a domain library crate that depends on no argument parser, terminal, environment or
  log-subscriber crate, and a CLI crate that holds parsing, composition, rendering and
  `main`. Any other project starts as one crate. Split further only when another such need
  appears.
- **R24.** *One definition per rule, enforced at the chokepoint.* Every validation,
  authorization and classification rule MUST have exactly one implementation, enforced in
  the shared path that every entry point crosses — CLI, HTTP or MCP handler, dashboard,
  scheduler, and any adapter added later. In practice that is the composition or domain
  operation that R2 routes surfaces through. A surface MUST NOT re-implement, pre-empt or
  bypass the rule, and two code paths that need the same rule call one function rather than
  spelling it twice.
- **R25.** *Obligations live in deterministic code.* Where a system hands work to an agent,
  a script or a person following instructions, an obligation the system later relies on (a
  record written, a field set, a check run) SHOULD be performed or verified by deterministic
  code, not left to prompt text, a README step or convention. Instructions may ask for it;
  code produces or checks it before anything depends on it.

### Visibility and dependencies

- **R8.** *Private by default.* Items MUST default to private or `pub(crate)`; `pub` is
  reserved for the deliberate cross-crate or public API, and the crate root re-exports only
  what a consumer actually needs.
- **R9.** *Workspace-declared dependencies.* In a workspace, every third-party dependency
  used by more than one member MUST be declared once in `[workspace.dependencies]` and
  referenced as `dep.workspace = true` (package `version`, `edition`, `rust-version` and
  `license` inherited the same way); a single crate pins `rust-version` in `[package]`.

### Errors and domain types

- **R10.** *Typed errors per unit; classify from structure.* Each crate (single crate: each
  layer module whose callers branch on its failures) MUST define its own
  `thiserror`-derived error enum with variants callers can match; library code never
  returns `String`, `Box<dyn Error>` or `anyhow::Error`. Any code that classifies a failure,
  its own or a child process's, MUST decide from structured data — an error variant, an exit
  status, a supervisor's verdict, an `io::ErrorKind` or errno value — and MUST NOT match
  substrings of a message, stderr, a URL or an errno's text.
- **R11.** *One surface error type.* The project MUST have one uniform error type at its
  surface boundary (Orbit: `OrbitError`), kept small by boxing large variant payloads. When
  that type is part of a public API it MUST be `#[non_exhaustive]`. In a two-crate CLI
  (R7) the surface error type is crate-private in the CLI crate, so `#[non_exhaustive]` has
  nothing to protect there; the domain library's public error enum (R10) carries it instead.
- **R12.** *Translate at the boundary, once.* A typed error that crosses into the surface
  error type MUST be converted by one translator and applied as `?` or
  `.map_err(translator)?`; call sites never map variants ad hoc, and code inside a unit
  propagates the typed error unchanged. When the owning unit can depend on the surface
  type (Orbit: `OrbitError` lives in the foundation crate `orbit-common`), the translator is
  defined next to the error in its owning unit. When the surface type lives in a crate that
  depends on the owning unit, as a CLI crate over a domain library does, the owning unit
  cannot name it; there the one translator is a single `#[from]` variant or `From` impl on
  the surface type, in the surface crate.
- **R13.** *No panics on fallible paths.* Production code MUST propagate failures with `?`
  instead of `unwrap()`, `expect()`, `panic!`, `todo!` or `unreachable!`; tests are exempt,
  and a genuine invariant `expect` carries a scoped `#[allow]` with a comment stating the
  invariant.
- **R14.** *Newtypes with validating constructors.* A primitive that carries a protocol
  contract (IDs, refs, versions, hashes, selectors) that crosses a function boundary SHOULD
  be a newtype with a private inner value and a fallible constructor (`new`/`FromStr`/
  `TryFrom`, plus `#[serde(try_from = …)]` when deserialized), not a bare `String` checked
  by an `is_valid_x(&str)` helper.
- **R26.** *Errors state verified causes, resolved inputs and a runnable remedy.* An error
  MUST assert only causes the code actually checked, and each failure mode MUST get its own
  error (variant and message), never a shared string that fits several. The message MUST
  carry the resolved inputs that decided the outcome: the path, scope, config layer or
  effective `PATH`. Any remedy it suggests MUST be safe and runnable from the surface where
  the error appears. How a surface presents the error is STD-01 §R19–R21.
- **R27.** *Exhaustive matches on domain enums.* Code that branches on a domain enum the
  project owns (a status, outcome, kind or class) MUST name every variant, with no wildcard
  or default arm, so that adding a variant fails the build at every decision it affects.
  *Rust:* no `_ =>` or catch-all binding arm on an in-workspace enum; write a predicate as
  an exhaustive `match` rather than `matches!` whenever a new variant's answer is not
  obviously "no". A foreign `#[non_exhaustive]` enum still needs its wildcard.
  *Elsewhere:* TypeScript's `never` check, Python's `typing.assert_never`.
- **R28.** *Configuration is validated at load.* Configuration and other operator-authored
  input MUST be validated when it is loaded: references resolved, numeric values checked
  against their bounds, and narrowing conversions checked (`try_from`, never `as`). A bad
  value is refused at load, naming the key, not discovered or silently wrapped at use time.
  Load and use go through the same validation function (R24).

### Failure semantics

- **R29.** *Unknown is never zero, empty, success or healthy.* When a value or a check
  cannot be obtained, the result MUST say so explicitly — `None`/`null` with a reason,
  `unavailable`, `outcome_unknown` — and MUST NOT read as 0, an empty list, "healthy",
  success or exit 0. "Could not run" and "ran and failed" are distinct outcomes. A health or
  readiness check MUST probe the real effective state (the timer is actually scheduled, the
  helper actually executes, the disk is actually writable), never only a configuration
  flag or a manager command's exit status.
- **R30.** *Distinct failure classes get distinct terminal states.* A wait, a dead end, an
  infrastructure failure and a verdict are different outcomes and MUST NOT share a terminal
  state, status or error variant. A caller can always tell "could not start" from "ran and
  was rejected", and "not yet" from "never".
- **R31.** *Fail closed on integrity, fail open on side channels, and say which.* When code
  cannot verify an authorization, sandbox, integrity or ownership fact, it MUST refuse. When
  a best-effort side channel fails (telemetry, metrics, cache refresh, cosmetic asset
  reconciliation), that failure MUST NOT fail the primary operation, and it is still
  recorded (R32). Each check MUST declare its class, in its type or a comment at the
  decision, so a reviewer can tell a deliberate fail-open from a missing error path. The
  security boundaries themselves are STD-05.
- **R32.** *One bad item is isolated, reported and counted.* In a batch, scan, listing or
  aggregate, a failing item MUST be isolated and reported with its identity and cause while
  the remaining items proceed. It MUST NOT abort the whole operation, and it MUST NOT be
  folded into a success summary or total: a run over N items with k failures reports k
  failures. An operation declared all-or-nothing instead refuses as a whole before its first
  side effect (R34).
- **R33.** *Recovery paths accept the states they exist to fix.* Resume, repair, recovery
  and cleanup paths MUST accept the partial, stale or half-applied states they were written
  for, and complete or undo them. They MUST NOT apply the forward path's strict
  preconditions and refuse. A state the path cannot identify with certainty is still
  refused, never guessed (STD-03 §R9).
- **R34.** *Preconditions before the first side effect.* An operation MUST validate every
  input and prerequisite — ids, flags, executables, capabilities, registered actions —
  before its first durable side effect, so a rejected request leaves nothing behind. A
  dry-run or preflight MUST run the same checks, in the same order, through the same code
  as the real run.

### Logging, contracts, and size

- **R15.** *`tracing`, not print.* Diagnostics MUST go through `tracing`. Only the CLI's
  single output layer, which owns stdout/stderr as the command's result channel (STD-01),
  may write to the standard streams: `print!`, `eprint!` and `dbg!` are denied everywhere,
  and outside that layer no code names `io::stdout()` or `io::stderr()` either. The one
  place that installs the log subscriber may hand it `io::stderr`; any other exception (an
  async-signal-safe write in a signal handler, say) is listed in the stream guard with its
  reason.
- **R16.** *Persisted and wire shapes are contracts.* A serde shape that is written to disk,
  a database, or another process MUST change only compatibly — new fields defaulted, a
  separate `*Document` wire type when the domain shape diverges, and a removed field or key
  warned about and ignored (or migrated) rather than silently rejected. Round trips MUST be
  lossless: a value that was written reads back as the same value, and neither a writer nor
  a deserializer fabricates a value it does not know. A missing timestamp stays absent
  (`Option`, never `#[serde(default = "Utc::now")]`), and an ambiguous record keeps its
  ambiguity instead of a guessed default.
- **R17.** *Fewest moving parts.* Dead code MUST be deleted together with its docs, tests,
  config keys and design notes; compatibility shims are kept only for an external contract
  or a persisted format (R16), and a retired path is guarded so it does not grow back.
- **R18.** *Bounded file size.* A source file SHOULD be split when it passes about 800
  lines — split along responsibilities, not into `part1`/`part2`.

### Tests

- **R19.** *Unit tests in a sibling `tests/` directory.* Unit tests for the files of a
  module MUST live in `<module>/tests/<source_file>.rs` (top-level files: `src/tests/`),
  declared once by the parent's `#[cfg(test)] mod tests;`, so they reach only
  `pub`/`pub(crate)`/`pub(super)` items; visibility is never widened solely for a test.
- **R20.** *Crate-root `tests/` is integration only.* The crate-root `tests/` directory MUST
  hold only tests that drive the crate's public API or built binary end to end.
- **R21.** *Tests exercise behaviour.* A test MUST assert what the code guarantees by running
  it; it never text-matches source, assets or UI copy (`include_str!` + `contains()`), and
  never pins operational policy owned by config or prompts (model, schedule, wording) unless
  its assertion message cites the incident it guards.

### Gates

- **R22.** *Shared lint baseline.* Every crate MUST opt into the lint table in
  [Machine checks](#machine-checks), enforced by CI running `cargo clippy --all-targets --
  -D warnings` so every `warn` lint fails the build.
- **R23.** *Supply-chain gate.* CI MUST run `cargo deny check` against a checked-in
  `deny.toml` that denies yanked crates, open advisories and unknown sources and allow-lists
  licenses, with every exception carrying a reason and a re-review date.

## Why

Decision citations of the form `area:n` point at
`codebases/orbit/docs/design/<area>/4_decisions.md` line *n*. Frictions are Orbit's unless
marked *(polaris)*.

- **R1.** A declared order turns "where does this go?" into a lookup. Orbit's 17 crates stay
  navigable because every crate sits on one named tier and "lower layers never depend on
  higher ones" is the whole rule; agents new to a repo follow a written order and violate a
  tacit one.
- **R2.** Orbit's CLI, MCP server, dashboard and agent tool hosts are four surfaces over the
  same operations. Where logic leaked into a surface it had to be re-plumbed per surface;
  ORB-10016 split the command layer out of the `orbit-core` god-crate into `orbit-cmd`
  (depending on Core, never the reverse) to get composition out of the kernel.
- **R3.** A unit that reads config or `$HOME` itself cannot be tested against a fixture root
  and silently diverges from what composition resolved. Orbit's config crate takes explicit
  roots, and its dependency check fails if Core's runtime calls `ResolvedConfig::load`.
- **R4.** A mechanism that imports a feature drags that feature into every consumer and
  creates cycles the moment the feature needs the mechanism. Orbit's `orbit-exec` and
  `orbit-policy` depend only on the two foundation crates.
- **R5.** Contract types are depended on by everything. If they do I/O, every consumer
  inherits side effects and heavy dependencies, and the types stop being safe to construct
  in tests. `orbit-types` does no I/O and its serde shapes double as persisted contracts.
- **R6.** An undocumented edge is how layering erodes: one convenient import at a time.
  Orbit's `check-dependency-direction.sh` rejects any edge not in its allowlist and any new
  crate without a policy; `AGENTS.md` makes a new edge an `ARCHITECTURE.md` change too.
- **R7.** Splitting by category multiplies manifests, re-exports and orphan-rule walls
  without buying isolation. Orbit's north-star bearing 5 records that crate splits are
  justified by compile-graph and dependency-direction needs; `orbit-store` stays one crate
  with a directional internal module graph checked by the same script. The two-crate
  default for a new CLI came from building the constellation Rust CLI template (ORB-13117):
  "the domain never imports clap or the terminal" is only machine-checkable as a manifest
  edge, and a single crate can only approximate it with grep bans.
- **R24.** A rule spelled twice drifts, and a guard placed on one surface is routed around
  by the next. Dashboard writes skipped `validate_for_set` (ORB-12744); a guard skipped the
  dashboard API (ORB-12199); a fix landed on the CLI while MCP still capped silently
  (ORB-12195); a `/healthz` variant skipped the Host gate, so DNS rebinding still worked
  (ORB-12531); one backend method sat outside `in_boundary` (ORB-12545); two selector
  grammars coexisted (ORB-11723); also ORB-12158 and F2026-07-116 ("nothing cross-checked
  the two"). Orbit's decisions say it plainly: "Two independent spellings of one rule is
  the defect" (activity-job:1062); a guard duplicated per adapter means "a third
  submission adapter would silently start without it" (activity-job:1229); any per-command
  guard "could be routed around by the next entry point" (operations-as-data:156); a
  duplicated table "could drift" (policy-sandbox:332). See also mcp-bridge:101 and
  auditability:153.
- **R25.** Prompt text is advice to a component that is free to skip it. F2026-07-102
  concluded "prompt text is the wrong enforcement layer" after three failed runs;
  F2026-07-125 found an agent instructed to produce an artifact it was structurally unable
  to produce; and when the execution summary was a prompt instruction, "agents skip it
  often, and every run that skipped it wedged at commit" (activity-job:1479), so Orbit now
  derives it from the change. SHOULD rather than MUST: many projects have no instruction
  layer, and some obligations cannot be computed.
- **R8.** Every `pub` item is surface someone else can couple to. Private-by-default keeps
  refactors local; ORB-10016 trimmed `orbit-core`'s root re-exports to the consumer-justified
  set for this reason.
- **R9.** One declaration per dependency keeps versions and features from drifting between
  members and makes upgrades a one-line change.
- **R10.** A typed enum lets callers branch on the failure (retry, report as input error,
  abort) instead of parsing messages. Stringly errors lose the kind at the first hop, and
  classifying by substring misfires as soon as wording changes or two classes share text:
  Orbit derived agent timeouts from stderr text until ORB-11701 moved them to the
  supervisor's verdict; ORB-12792 was a URL substring match; F2026-09-151 found the same
  errno text for opposite classes; also ORB-11802 and ORB-12335.
- **R11.** Surfaces map errors to exit codes, HTTP statuses and audit outcomes. One
  surface type means one mapping; `OrbitError` is `#[non_exhaustive]` and carries a size
  budget because it rides in every `Result` in the workspace. The two-crate CLI note came
  from the template build (ORB-13117), where the surface type is private and the attribute
  protects nothing.
- **R12.** Centralising the kind→variant mapping in one place keeps the surface error
  coherent and lets internal code keep the rich type. ORB-10013 added
  `check-error-translation.sh` after a caller crate (`orbit-mcp`) grew a private translator
  for another crate's error. The downstream-surface form was added after the template
  build (ORB-13117) found that "next to the owning error" is impossible when the owner
  cannot depend on the surface crate.
- **R13.** A panic at a boundary turns a recoverable condition (bad input, missing file,
  lost race) into a crash with no error path for the caller or the surface. Orbit keeps
  `unwrap_used`/`expect_used` on for production and exempts tests with `cfg_attr(test)`.
- **R14.** Validate-then-use leaves every callsite to remember the check and admits
  time-of-check/time-of-use bugs; a validated type carries the proof. Orbit's own
  `docs/design-patterns/newtype.md` records that it has no production private-inner
  validated newtype today and still uses `validate_*(&str)` helpers for identities — a gap,
  hence SHOULD rather than MUST.
- **R26.** "An error that names causes it has not checked converts a one-command fix into a
  diagnosis cycle" (F2026-07-112). Orbit's shared commit-step message "cost three diagnosis
  cycles, two of which reached confidently wrong conclusions" (activity-job:1029), and its
  replacement "asserts no cause it did not measure" (activity-job:1022). A lost index was
  reported as `invalid_input` (ORB-12172); recovery advice that was itself destructive
  (ORB-12131) shows why a remedy must be safe; F2026-07-055 asked for the effective `PATH`
  in the message. Also ORB-10927, F2026-08-106 and policy-sandbox:315 (fail "at a layer that
  knows why").
- **R27.** A wildcard arm silently files every future variant under whatever the default
  was. Orbit's dependency semantics partition `TaskStatus` between two predicates, and its
  decision records the cost: a variant added to neither "is silently treated as a
  legitimate wait forever" (activity-job:1270), which an exhaustive match turns into a build
  error. ORB-10958 is the same shape outside an enum: a redaction allowlist missed new
  writers.
- **R28.** A value that is only checked when it fires fails at the worst time and far from
  its source. ORB-11734 rejected an out-of-range `every_minutes` whose slot arithmetic would
  otherwise overflow; ORB-11726 found a large YAML value that could panic the whole sweep;
  "load-time validation makes a broken reference visible on the next sweep instead of at
  fire time" (routines:106).
- **R29.** A missing value rendered as zero or healthy is a false statement that nobody
  investigates. Orbit's dashboard rendered unavailable failure metrics "as a measured zero"
  (ORB-11201, ORB-11207); tasks vanished silently (ORB-12151); a panicked worker was dropped
  (ORB-11605); doctor reported healthy while a default was missing (ORB-11791); the parent
  could not tell a review that never started from a failed one (ORB-10606); a process "still
  exited 0" (F2026-07-002) or reported "`completed: true` while having done nothing"
  (F2026-07-155); a silent null "provided no diagnostic" (F2026-07-008); and in F2026-08-017
  a merge went through unreviewed. Also ORB-12335, F2026-07-167 and F2026-08-047. The
  decisions: "0% can never stand in for no data" (user-interface:205); "a probe that cannot
  answer must never be read as proof of death" (auditability:492); unpriced models "degrade
  to unknown rather than a silently wrong number" (auditability:435). Health checks that
  read flags fail the same way: "enabled state and successful manager command exits are
  insufficient for health" (routines:208); the sandbox preflight "actually executes the
  sandbox helper rather than merely checking that the binary is installed"
  (policy-sandbox:294); a review was never minted across about 14 deliveries while nothing
  alarmed (F2026-09-200); a full disk froze the server (F2026-09-122).
- **R30.** An operator who cannot tell a wait from a dead end polls forever, and one who
  cannot tell an infrastructure failure from a verdict re-litigates the verdict. Orbit's
  admission now refuses dead-end dependencies instead of waiting on them
  (activity-job:1251), and review startup failure is classified separately from reviewer
  rejection so operators can distinguish them "without opening the child run"
  (activity-job:808); ORB-10606 is the incident.
- **R31.** Guessing through unverifiable security state converts an error into a breach;
  failing the main operation on a telemetry write converts a cosmetic gap into lost work.
  Fail closed: a program allowlist failed open when an activity omitted it (ORB-10959); in
  F2026-09-203 a failed `/proc` read resolved to "ordinary local caller with no allowlist"
  for about 50 minutes; also ORB-11514 and ORB-12789. Orbit's decisions: "all ambiguous or
  changed remote state fails closed" (activity-job:974); "Durable registry input fails
  closed" (host-registry:57); "Orbit verifies it and refuses; it does not silently degrade"
  (policy-sandbox:307). Fail open: "never fail a run on a telemetry write" (commit
  f7af3890f, ORB-10367); fail open when managed-asset reconciliation cannot write a
  read-only manifest (ORB-10840).
- **R32.** One malformed entry should cost one entry. The dashboard refused to start over
  one bad workspace (ORB-11386); one `/proc` failure aborted every runtime open (ORB-12607);
  one large YAML value could panic the whole sweep (ORB-11726); an unavailable systemd bus
  produced a 500 (ORB-12517); also F2026-07-098. The opposite failure hides the loss: a
  polaris security audit reported a clean auth section because a date-format defect made a
  sub-check fail silently (*(polaris)* F2026-07-021, F2026-07-024), and ORB-11801 dropped all
  subprocess output.
- **R33.** A recovery path that demands a clean state strands exactly the cases it exists
  for. A rebase-recovery checkpoint "hard-fails every resume instead of redo[ing]" the rebase
  (ORB-12015); ORB-12575 is the same shape; ORB-12128 stranded an already-disabled tool.
- **R34.** Validation after the first write leaves a half-built state for the next run to
  trip over. `orbit init` seeded the global root before validating `--task-prefix`,
  "leaving a partial root on failure" (ORB-12112); an update failed "only after the
  executable is replaced" (ORB-12612); a pipeline merged "to agent-main before it enforces
  the execution_summary precondition" (F2026-07-014); a dry run disagreed with the real tick
  (ORB-12337). Also ORB-10683, F2026-07-055 and F2026-07-116. An incompatible failure hook
  was discovered "*inside* it … the worst possible time" (activity-job:1042); the sandbox
  preflight now runs the real helper before dispatch (policy-sandbox:294).
- **R15.** `print!` bypasses filtering, structure, redaction and rotation, and corrupts
  stdout for callers parsing command output. Orbit denies print lints in library crates and
  allows them only in the CLI binary, with a comment saying why. The template build
  (ORB-13117) found that `clippy::print_stdout` does not see `writeln!(io::stdout(), …)`, so
  the lint alone does not keep stdout in one module; the stream names are banned too.
- **R16.** Persisted state outlives the binary that wrote it: an older or newer binary will
  read it. Orbit keeps the lexical index file named `semantic.db` so persisted paths stay
  valid, and warns about and ignores removed config keys so an existing `config.toml` keeps
  loading. A fabricated value is worse than a missing one because nothing marks it as a
  guess: Orbit stamped `Utc::now()` into `created_at`/`updated_at` on every read of a
  record that lacked them (ORB-11735); `ActorIdentity` did not round-trip losslessly
  (ORB-11731); a `pr_number` flipped between string and number (ORB-12640). Ambiguous
  evidence should "honestly preserve uncertainty instead of fabricating an exit"
  (auditability:513).
- **R17.** Dormant code still has to be understood by every reader of the paths it sits in.
  Orbit removed the unused operation-mode authorization layer end to end (UI, CLI, Core,
  config keys, tables, design folder) in ORB-12769..12772 rather than keep it behind a
  default, keeping only the load-time warning for removed keys; its dependency check fails
  if a retired module path reappears.
- **R18.** Large files hide multiple responsibilities and make review diffs unreadable.
  Orbit treats ~800 lines as a signal to split, not a hard limit (23 non-test files exceed
  it today), so the rule is SHOULD.
- **R19.** A sibling test module is not a child of the source module, so it cannot reach
  private items: the file layout itself enforces "test through the exposed seam" and keeps
  `#[cfg(test)]` noise out of production listings. F2026-08-108 found test files that were
  never declared in `tests/mod.rs` and silently compiled to nothing; Orbit now checks module
  reachability.
- **R20.** Cargo compiles each crate-root test as a separate binary against the public
  surface — right for end-to-end tests, wrong (and slow) for unit tests.
- **R21.** A text-matching test breaks on every wording edit and proves nothing runs; a test
  pinning crew, model or schedule turns every ops edit into red CI. Orbit's agent guide bans
  both, with a narrow exception for structural safety guards that name what they protect.
- **R22.** The lint table is the part of these rules a compiler can check. `warn` keeps
  local iteration unblocked; `-D warnings` in CI makes it binding.
- **R23.** Advisory, license and source drift enters through `Cargo.lock` without anyone
  editing code. Orbit gates it in CI (ORB-00416, ORB-11983) with time-boxed, justified
  exceptions.

## Exemplars

Paths are relative to `codebases/orbit/`.

- R1 — `ARCHITECTURE.md#architecture` (tier diagram and rule); `ARCHITECTURE.md#crates` (per-crate table)
- R2 — `ARCHITECTURE.md#application-and-surfaces`; `docs/design/orbit-core/4_decisions.md#extract-the-cli-facing-command-layer-into-orbit-cmd`
- R3 — `ARCHITECTURE.md#domain` (orbit-config: "callers pass explicit roots"); `scripts/check-dependency-direction.sh` lines 201–206 (`ResolvedConfig::load` ban in Core runtime)
- R4 — `ARCHITECTURE.md#foundation-and-kernel`; `scripts/check-dependency-direction.sh#allowed_internal_deps` (`orbit-policy | orbit-exec` → common, types only)
- R5 — `ARCHITECTURE.md#foundation-and-kernel` (orbit-types: "does no I/O of any kind"); `crates/orbit-types/Cargo.toml` `[dependencies]`
- R6 — `scripts/check-dependency-direction.sh` (crate allowlist lines 8–84; single-crate module rules lines 183–234); `AGENTS.md#rules` ("don't add cross-crate dependencies without updating ARCHITECTURE.md"); `crates/orbit-mcp/tests/dep_boundary.rs#mcp_depends_only_on_its_leaf_domains`
- R7 — `docs/design/orbit-core/4_decisions.md#north-star-architecture-bearing-operations-as-data-behind-an-operation-registry` (bearing 5); `ARCHITECTURE.md#orbit-store-internals`
- R24 — `docs/design/activity-job/4_decisions.md#ship-duplicate-dispatch-guard-lives-in-the-shared-submission-path`; `crates/orbit-core/src/application/job/pipeline/submit.rs#submit_ship_run` (its guard `in_flight_ship_run_for_tasks` reads the persisted run input, so every surface is covered); `docs/design/operations-as-data/4_decisions.md#capability-chokepoint-for-destructive-operations-outside-mcp`; `crates/orbit-automation/src/auto_tasks/schedule.rs#interval_period` ("the single range rule" shared by validation and both slot calculations)
- R25 — `docs/design/activity-job/4_decisions.md#derive-the-delivery-execution-summary-from-the-change-not-from-the-agent`; `crates/orbit-engine/src/executor/automation/vcs/commit/summary.rs#ensure_durable_execution_summary`
- R8 — `AGENTS.md#code` ("Default to `pub(crate)`"); `crates/orbit-store/src/lib.rs` module declarations (`pub mod contracts`, private `mod driver`, `mod repository`)
- R9 — `Cargo.toml#workspace.dependencies` and `#workspace.package`; `crates/orbit-types/Cargo.toml` (`serde.workspace = true`, `version.workspace = true`)
- R10 — `crates/orbit-automation/src/error.rs#AutomationError`; `crates/orbit-types/src/plugin/version.rs#VersionError`; `crates/orbit-agent/src/types/response/envelope.rs#is_timeout` (reads the supervisor's `timed_out` flag, not stderr)
- R11 — `crates/orbit-common/src/error.rs#OrbitError` (`#[non_exhaustive]`, 128-byte size budget)
- R12 — `docs/design-patterns/error_translation.md#crate-boundary-error-translation`; `crates/orbit-automation/src/error.rs#automation_error_to_orbit`; `crates/orbit-engine/src/activity_job/dispatcher.rs#dispatch_error_to_orbit`; `scripts/check-error-translation.sh`
- R13 — `Cargo.toml#workspace.lints.clippy` (`unwrap_used`, `expect_used`); `crates/orbit-common/src/lib.rs` line 3 (`cfg_attr(test, allow(...))`)
- R14 — `docs/design-patterns/newtype.md#newtype-wrapper`; `crates/orbit-types/src/plugin/version.rs#Version` (`FromStr` + `#[serde(try_from = "String")]`); `crates/orbit-core/src/application/automation/preparation.rs#InstructionSnapshot` (private inner)
- R26 — `crates/orbit-engine/src/executor/automation/vcs/commit/mod.rs#empty_stage_error` (reports observed staged/unstaged/untracked counts, HEAD and base; shares no wording with `unrelated_history_error`); `docs/design/activity-job/4_decisions.md#pipeline-steps-consume-a-base-commit-pinned-at-worktree-setup-never-a-moving-ref-name`
- R27 — `crates/orbit-types/src/task/model/status.rs#dependency_dead_end` (every `TaskStatus` variant named, no wildcard); `docs/design/activity-job/4_decisions.md#dispatch-admission-separates-unmet-dependencies-from-unsatisfiable-ones`
- R28 — `crates/orbit-automation/src/auto_tasks/schedule.rs#validate_schedule` and `#interval_period` (range check, then `i64::try_from`); `docs/design/routines/4_decisions.md` line 106
- R29 — `crates/orbit-core/src/metrics/reliability.rs#Rate` (`value` is `None` when the denominator is zero); `crates/orbit-core/src/application/routines/clock/inspect.rs#ClockUnitVerdict` (probes the unit's real program; `Unrunnable { reason }` is its own verdict); `docs/design/routines/4_decisions.md#host-local-sweep-clock-configuration`
- R30 — `docs/design/activity-job/4_decisions.md#classify-independent-review-startup-separately-from-reviewer-rejection`; `crates/orbit-types/src/task/model/status.rs#dependency_dead_end`
- R31 — `docs/design/host-registry/4_decisions.md#durable-registry-input-fails-closed`; `docs/design/policy-sandbox/4_decisions.md#sandbox-availability-is-a-host-precondition-not-a-runtime-fallback`; `crates/orbit-engine/src/activity_job/audit_writer.rs#note_telemetry_failure` (fail-open, documented as such at the decision)
- R32 — `crates/orbit-web/src/state/registry.rs#UnavailableCheckout` and `#report_unavailable` (one broken checkout is warned about and skipped; healthy workspaces keep serving); `crates/orbit-engine/src/activity_job/audit_writer.rs#note_telemetry_failure` (the failure is counted and recorded on the run, not dropped)
- R33 — `crates/orbit-engine/src/executor/automation/vcs/freshness.rs#discard_unauthenticated_rewrite` and `#perform_rebase_onto_base` (an uncertified pre-boundary state is restored and redone, not refused)
- R34 — `crates/orbit-cli/src/command/init/command.rs#reject_invalid_fresh_identity_inputs` (runs before `orbit init` writes anything); `docs/design/activity-job/4_decisions.md#the-runtime-reports-its-deterministic-action-registry-and-job-validation-gates-on-it`; `docs/design/policy-sandbox/4_decisions.md#sandbox-availability-is-a-host-precondition-not-a-runtime-fallback`
- R15 — `crates/orbit-common/src/lib.rs` line 1 (`#![deny(clippy::print_stderr, clippy::print_stdout)]`); `crates/orbit-cli/src/main.rs` lines 3–4 (the binary's print allow, with its reason); `crates/orbit-common/src/observability/logging.rs#init_default_subscriber`
- R16 — `ARCHITECTURE.md#foundation-and-kernel` ("serde shapes are persisted contracts"); `crates/orbit-types/src/task/plan.rs#TaskPlanDocument`; `crates/orbit-config/src/registry/keys.rs#REMOVED_CONFIG_KEYS`; `crates/orbit-types/src/policy/policy_def.rs#PolicyDef` (`created_at: Option<DateTime<Utc>>`, no `Utc::now` default); `crates/orbit-types/src/identity/actor.rs#agent_label_round_trips` (switches to the tagged form when a bare label would read back as another variant)
- R17 — `AGENTS.md#code` ("Prefer the fewest moving parts"); `docs/design/orbit-core/4_decisions.md#remove-operation-mode-rather-than-keep-an-unused-authorization-layer`; `scripts/check-dependency-direction.sh` (retired ownership paths, lines 236–256)
- R18 — `AGENTS.md#code` ("~800 lines per file is a split signal")
- R19 — `docs/design-patterns/test_layout.md#per-module-sibling-tests-directory`; `crates/orbit-mcp/src/adapter/tests/`; `scripts/check-orphan-modules.sh`
- R20 — `docs/design-patterns/test_layout.md#when-not-to`; `crates/orbit-cli/tests/`
- R21 — `AGENTS.md#rules` (text-matching ban); `docs/design-patterns/test_layout.md#what-tests-assert-invariants-vs-policy`
- R22 — `Cargo.toml#workspace.lints.clippy`; `Makefile#ci-lint`; `scripts/ci-guardrails.sh` (`cargo clippy --workspace --all-targets -- -D warnings`)
- R23 — `deny.toml`; `scripts/cargo-deny.sh`; `Makefile#audit`

## Machine checks

| Rule | Gate |
|---|---|
| R1 | Dependency-direction check (below) |
| R2 | Dependency-direction check (surface and transport crates/modules forbidden in domain); otherwise review-only |
| R3 | Dependency-direction check: grep ban on config-loading and root-discovery calls outside composition |
| R4 | Dependency-direction check |
| R5 | Dependency-direction check (no internal edges) plus a grep ban on `std::fs`/`std::net`/`std::process` in the contract unit |
| R6 | Dependency-direction check in CI (fails on an unlisted edge or an unlisted crate) |
| R7 | Dependency-direction check bans surface crates (`clap`, terminal and log-subscriber crates) in the domain crate; the split decision itself is review-only |
| R24 | A regression test that drives the rule through the shared function rather than through one surface (Orbit: the ship guard's test calls `submit_ship_run` directly); otherwise review-only |
| R25 | review-only |
| R8 | review-only (optionally `unreachable_pub = "warn"`, not in Orbit's baseline) |
| R9 | review-only (Orbit does not gate it; see Deviations note below) |
| R10 | review-only; an optional grep for `.contains(` on `stderr`, error `to_string()` or message values in classification code is a useful review aid |
| R11 | review-only |
| R12 | Error-translation check (registry of boundary errors → translator in owning unit; no `FooError::X => SurfaceError::Y` at call sites) — adopt once there are two or more translators |
| R13 | `clippy::unwrap_used`, `clippy::expect_used` (+ `-D warnings`) |
| R14 | review-only |
| R26 | review-only; a unit test per error constructor can assert that the resolved inputs appear in the message |
| R27 | Adoptable lints `clippy::wildcard_enum_match_arm` and `clippy::match_wildcard_for_single_variants` (see [Adoptable additions](#adoptable-additions-beyond-orbits-baseline)); not in Orbit's baseline |
| R28 | A load-time test per bounded key (an out-of-range value is refused at load, naming the key); adoptable lints `clippy::cast_possible_truncation`, `clippy::cast_sign_loss`, `clippy::cast_possible_wrap` for the `as` ban; not in Orbit's baseline |
| R29 | Tests that make the source unavailable or failing and assert the explicit unknown state (not 0, empty, healthy or exit 0); a health check's test runs against a stopped or broken target; otherwise review-only |
| R30 | review-only (R27's exhaustive matches keep the outcome enum honest) |
| R31 | A negative test per fail-closed check (unverifiable input → refusal) and per fail-open channel (side-channel failure → primary operation succeeds and the failure is recorded); the class declaration is review-only |
| R32 | A test with one poisoned item among good ones: the good items complete, the bad one is reported by identity, and the summary counts it as a failure |
| R33 | A test per documented partial state that seeds it and asserts the recovery path completes or undoes it |
| R34 | A test per rejected input asserting the fixture root is unchanged afterwards; dry-run/real-run code sharing is review-only |
| R15 | `clippy::print_stdout`, `clippy::print_stderr`, `clippy::dbg_macro`, plus a stream grep guard (below): clippy does not see `writeln!(io::stdout(), …)` |
| R16 | Round-trip tests on persisted fixtures (old document deserializes; removed key warns; write → read → write is byte-stable); grep ban on `serde(default = "…now")` |
| R17 | Retired-path guard in the dependency-direction check; otherwise review-only |
| R18 | review-only |
| R19 | Orphan-module check (every `src/**/tests/*.rs` declared in its `tests/mod.rs`) |
| R20 | review-only |
| R21 | review-only |
| R22 | `cargo clippy --all-targets -- -D warnings` in CI and in the pre-handoff gate |
| R23 | `cargo deny check` in CI |

### Lint baseline (R13, R15, R22)

This is Orbit's complete `[workspace.lints]` table, copied from `codebases/orbit/Cargo.toml`
(lines 44–55) as of 2026-09-26 — not a subset. In a workspace root `Cargo.toml`:

```toml
# Workspace-wide lint config. Each crate opts in via `[lints] workspace = true`.
# Keep this list short — only rules that are mechanically true everywhere.
[workspace.lints.clippy]
await_holding_lock = "deny"
dbg_macro = "deny"
expect_used = "warn"
print_stderr = "warn"
print_stdout = "warn"
unwrap_used = "warn"

[workspace.lints.rust]
missing_docs = "warn"
```

and in every member's `Cargo.toml`:

```toml
[lints]
workspace = true
```

For a single-crate project, put the same keys in the package `Cargo.toml` as
`[lints.clippy]` and `[lints.rust]`. `await_holding_lock` is STD-03 territory; it is kept
here because it is part of the same table.

Crate-root attributes that complete the baseline (from `crates/orbit-common/src/lib.rs` and
`crates/orbit-cli/src/main.rs`):

```rust
// Library crates / non-output modules: printing is an error, not a warning.
#![deny(clippy::print_stderr, clippy::print_stdout)]
// Unit tests use unwrap/expect for fixture setup; production call sites remain linted.
#![cfg_attr(test, allow(clippy::expect_used, clippy::unwrap_used))]
```

In a new binary, deny the print lints crate-wide too and have the one output module write
through locked handles (`writeln!(io::stdout().lock(), …)`) that return `io::Result`. A
closed pipe then arrives as an `io::ErrorKind::BrokenPipe` value the output layer maps to a
silent exit 0 (STD-01 §R13), so the binary needs neither a scoped print allow nor a
broken-pipe panic hook. Because clippy's print lints do not see those `writeln!` calls, pair
them with the stream grep guard below. Orbit instead allows printing crate-wide in
`main.rs` and allows `missing_docs` at 15 of 17 crate roots, so in practice `missing_docs`
is enforced only in `orbit-config` and `orbit-web`. A new repo keeps `missing_docs` on from
the start. Crate-root integration tests (`tests/*.rs`) need their own
`#![allow(clippy::expect_used, clippy::unwrap_used)]`.

CI and the pre-handoff gate run:

```sh
cargo fmt --all -- --check
cargo clippy --workspace --all-targets -- -D warnings   # single crate: drop --workspace
```

### Adoptable additions (beyond Orbit's baseline)

These lints are **not** in Orbit's table and are not part of R22's required baseline. They
mechanize R27 and R28 for a repo that wants the gate; each name was checked with
`cargo clippy --explain <lint>` on clippy 0.1.96. Add them to the same
`[workspace.lints.clippy]` table (single crate: `[lints.clippy]`):

```toml
# STD-02 §R27 — no wildcard arm on an enum match (restriction group; also fires on
# foreign enums, so scope an #[allow] with a reason where a wildcard is required).
wildcard_enum_match_arm = "warn"
match_wildcard_for_single_variants = "warn"
# STD-02 §R28 — narrowing `as` casts must be checked conversions (pedantic group).
cast_possible_truncation = "warn"
cast_sign_loss = "warn"
cast_possible_wrap = "warn"
```

And a grep ban for R16's "no fabricated timestamps", suitable for the same CI job:

```sh
if grep -rnE --include='*.rs' 'serde\(default = "(chrono::)?Utc::now"' crates; then  # single crate: src
  echo "fabricated timestamp default (STD-02 §R16)" >&2; exit 1
fi
```

### Stream grep guard (R15)

Only the output module may name the standard streams. A minimal guard, assuming that module
is `crates/<tool>/src/output/` (single crate: `src/output/`):

```sh
#!/usr/bin/env bash
# scripts/check-terminal-guard.sh — STD-02 §R15 (and STD-01's stream rules).
set -euo pipefail
cd "$(dirname "$0")/.."
OUTPUT_DIR=crates/mytool/src/output
PATTERN='io::stdout|io::stderr|println!|eprintln!|print!|eprint!|is_terminal|IsTerminal'
hits=$(grep -rnE --include='*.rs' --exclude-dir=tests "$PATTERN" crates \
  | grep -v "^$OUTPUT_DIR/" | grep -vE '^[^:]+:[0-9]+:[[:space:]]*//' || true)
if [[ -n "$hits" ]]; then echo "$hits" >&2; echo "std streams belong to $OUTPUT_DIR only" >&2; exit 1; fi
```

The constellation Rust CLI template (`operations/templates/rust-cli/` in the constellation
root) ships the full version as `scripts/check-terminal-guard.sh`. List any justified
exception (R15) as an extra `grep -v` with a comment giving its reason.

### Dependency-direction check (R1–R7, R17)

**Workspace, from `cargo metadata`.** Port the shape of
`codebases/orbit/scripts/check-dependency-direction.sh`: an explicit allowlist of internal
dependencies per crate, read from `cargo metadata --no-deps`, that fails on (a) a workspace
crate with no policy entry, (b) any internal edge not on that crate's list, and (c) a
dev-only edge used as a normal dependency. Keep the layer table in the architecture doc and
the allowlist in the same change (R6). Orbit additionally pins a few crates with a
manifest-parsing test (`crates/orbit-mcp/tests/dep_boundary.rs`); the script alone is
sufficient. This variant needs cargo and a JSON parser.

**Workspace, from the manifests alone.** For a small workspace, or a fast CI lane that runs
before anything compiles, read the non-dev dependency tables straight out of each
`crates/*/Cargo.toml` with `awk`. It needs no build, no network and no `jq`:

```sh
#!/usr/bin/env bash
# scripts/check-dependency-direction.sh — STD-02 §R1–R7 from Cargo.toml files only.
set -euo pipefail
cd "$(dirname "$0")/.."
PREFIX=mytool; fail=0
err() { echo "dependency-direction: $*" >&2; fail=1; }
policy() { # sets ALLOWED (internal deps) and BANNED (external deps); unknown crate = no policy
  case "$1" in
    mytool-core) ALLOWED="";            BANNED="clap tracing-subscriber anstream crossterm" ;;
    mytool)      ALLOWED="mytool-core"; BANNED="" ;;
    *) return 1 ;;
  esac
}
declared_deps() { # names in [dependencies], [build-dependencies] and target-specific tables
  awk '/^\[/ { in_deps = ($0 ~ /^\[(target\..*\.)?(build-)?dependencies\]$/); next }
       in_deps && /^[A-Za-z0-9_-]+[[:space:]]*(\.workspace)?[[:space:]]*=/ {
         n = $1; sub(/\.workspace$/, "", n); sub(/=.*/, "", n); gsub(/[[:space:]]/, "", n); print n }' "$1"
}
has() { local w; for w in $2; do [[ $w == "$1" ]] && return 0; done; return 1; }
for manifest in crates/*/Cargo.toml; do
  crate=$(awk -F'"' '/^name[[:space:]]*=/ { print $2; exit }' "$manifest")
  policy "$crate" || { err "crate '$crate' has no policy"; continue; }
  while IFS= read -r dep; do
    if [[ $dep == "$PREFIX" || $dep == "$PREFIX"-* ]]; then
      has "$dep" "$ALLOWED" || err "$crate must not depend on internal crate $dep"
    elif has "$dep" "$BANNED"; then err "$crate must not depend on $dep"; fi
  done < <(declared_deps "$manifest")
done
exit "$fail"
```

It does not see dependencies declared in dotted-key form (`dependencies.foo = …`) or
inside an inline table on one line; keep manifests in the conventional one-table-per-section
shape, or use the `cargo metadata` variant. Module bans (the single-crate block below) can
follow in the same script, as the Rust CLI template's
`scripts/check-dependency-direction.sh` does.

**Single crate.** Layers are modules, so the check is a set of `rg` bans over
`src/`, excluding test code. A minimal version for `domain` / `app` / `cli` layers:

```sh
#!/usr/bin/env bash
# scripts/check-dependency-direction.sh — STD-02 §R1–R6 for a single-crate CLI.
set -euo pipefail
cd "$(dirname "$0")/.."
fail=0
ban() { # ban <dir> <pattern> <message>
  if rg -n "$2" "$1" -g '*.rs' -g '!**/tests/**'; then echo "$3"; fail=1; fi
}
ban src/types  'crate::(domain|app|cli)|std::(fs|net|process)' "types must be I/O-free leaves"
ban src/domain 'crate::(app|cli)|\bclap\b'                     "domain must not import app or surface code"
ban src/app    'crate::cli'                                     "composition must not import surfaces"
ban src/domain 'std::env::(current_dir|home_dir|var)'           "domain receives resolved roots and config"
for retired in src/legacy; do [[ -e "$retired" ]] && { echo "retired path exists: $retired"; fail=1; }; done
exit "$fail"
```

Wire it into the same CI job as clippy. Orbit's `orbit-core` and `orbit-store` blocks in its
script (lines 183–234) are the multi-module version of exactly this.

### cargo-deny (R23)

Install a pinned `cargo-deny`, check in `deny.toml`, and run `cargo deny check` in CI. The
policy sections below are copied from `codebases/orbit/deny.toml` (subset: Orbit's
`ignore` entry and license comments are omitted — start with an empty `ignore` and add the
licenses your tree actually needs):

```toml
[advisories]
yanked = "deny"
ignore = [
    # { id = "RUSTSEC-YYYY-NNNN", reason = "<why it does not apply>. Re-review YYYY-MM-DD." },
]

[licenses]
allow = ["MIT", "Apache-2.0", "Apache-2.0 WITH LLVM-exception", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unicode-3.0"]
confidence-threshold = 0.9

[bans]
multiple-versions = "allow"
wildcards = "allow"

[sources]
unknown-registry = "deny"
unknown-git = "deny"
```

A sandboxed or offline runner needs a writable advisory DB; Orbit's
`scripts/cargo-deny.sh` shows the wrapper (`--disable-fetch`, custom `db-path`).

## Applies to

- `rust`: every rule, in every constellation Rust project, whether a multi-crate workspace
  or a single-crate CLI; all gates in [Machine checks](#machine-checks).
- `universal`: R24–R26 and R29–R34, the classification clause of R10 and the lossless
  clause of R16 are language-neutral and bind any project; R27 and R28 wherever the
  language offers exhaustiveness checking and checked conversions.
- `cli`: R7's two-crate default, R11 and R12's downstream-surface form, and R15's stream
  guard.
- `service`: R29 (health checks), R31 and R32 bind hardest on long-running services.
- `stateful`: R16, R33 and R34 for anything that persists state across runs.

Out of scope, and optional for adopters: Orbit's per-crate stability tiers
(`[package.metadata.orbit] stability`, `codebases/orbit/scripts/check-stability.sh`) and its
product-identity marker (`codebases/orbit/ARCHITECTURE.md#product-identity`) are
Orbit-specific mechanisms. A repo with external crate consumers can adopt a stability
marker; nothing here requires it. Concurrency, async, locking, bounded channels and
subprocess safety are STD-03.

## Deviations

An adopting repo that departs from a rule records a decision in its own design docs, citing
`STD-02@<version> §Rn` (for example `STD-02@2 §R27`) and the reason. It never edits its
vendored copy of this standard.

Known deviations in the exemplar itself, so adopters do not copy them: Orbit has no
production private-inner validated newtype (R14) and validates identities with
`validate_*(&str)` helpers; several Orbit crates declare `tempfile = "3"` directly although
it is in `[workspace.dependencies]` (R9); `missing_docs` is allowed at most crate roots
(R22); Orbit's `satisfies_dependency` is a `matches!` predicate rather than an exhaustive
match (R27); Orbit gates none of the [adoptable lints](#adoptable-additions-beyond-orbits-baseline);
and Orbit has no stream grep guard (R15): besides its logging subscriber, `orbit-exec`
writes stderr from a signal handler and a debug tee, and `orbit-common` probes stderr for a
terminal.

## Changelog

- **2** (2026-09-26, ORB-13126).
  - Normative, new rules from `projects/standards/tacit-extraction-2026-09-26.md`
    (disposition on ORB-13088): R24 one definition per rule at the chokepoint (T6); R25
    obligations in deterministic code, SHOULD (A2); R26 errors state verified causes,
    resolved inputs and a runnable remedy (T13); R27 exhaustive matches on domain enums
    (A5); R28 config validated at load with checked conversions (E4); R29 unknown never
    reads as zero, empty, success or healthy, and health checks probe real state (T2 with
    O2); R30 distinct failure classes get distinct terminal states (E3); R31 fail closed on
    integrity, fail open on side channels, declared per check (T5); R32 one bad item is
    isolated, reported and counted (E2 with E6); R33 recovery paths accept the states they
    exist to fix (E5); R34 preconditions before the first side effect, with preflight
    sharing the real run's checks (T15).
  - Normative, extended rules: R10 classifies failures from structured data, never message
    or stderr substrings (E1). R16 requires lossless round trips and forbids fabricating an
    unknown value on write or deserialize (P4).
  - Normative, clarified from frictions found building the constellation Rust CLI template
    on v1 (ORB-13117): R7 says a new CLI SHOULD start as two crates (domain library plus CLI
    surface), while other projects still start as one. R11 requires `#[non_exhaustive]`
    only when the surface type is public. R12 accepts a single `#[from]`/`From` impl on a
    downstream surface type as the one translator. R15 bans naming `io::stdout()` or
    `io::stderr()` outside the output layer, with a stream grep guard.
  - Editorial: plain-line cluster labels became `###` headings; the Errors cluster is now
    "Errors and domain types", and a new "Failure semantics" cluster holds R29–R34.
    "Applies to" is a tag list with rule scopes. The lint guidance recommends locked-handle
    writes over a scoped print allow. Machine checks gained adoptable lints (beyond Orbit's
    baseline), a stream grep guard and a manifest-only dependency-direction variant. The
    lint baseline block is unchanged.
- **1** (2026-09-26). First publication: R1–R23.
