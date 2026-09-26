---
id: STD-04-testing-and-verification
title: Testing and verification — test design, hermetic tests, honest gates, docs as contracts, and evidence-based verification conduct
summary: Normative rules for how a change is tested and verified — regression tests that drive the real entry point and fail before the fix, no weakened guards, behaviour-only and hermetic tests, visible skips, root-caused flakes, gates that cannot pass vacuously, regenerated artifacts, docs claims and examples as contracts, and the evidence whoever makes the change (human or agent) must produce before calling it done; follow it in any project with tests, CI or docs.
status: active
tags: [standard, testing, verification, ci, docs, agents]
version: 1
created: 2026-09-26
updated: 2026-09-26
last_validated: 2026-09-26
related: [STD-01-cli-surface, STD-02-rust-architecture-and-errors, STD-03-concurrency-and-process-safety, STD-05-security-boundaries]
---

# Testing and verification

These rules cover how a change proves it works: what a test must exercise, what keeps a
test honest and hermetic, what makes a gate trustworthy, when documentation counts as a
contract, and what evidence the author of a change owes before calling it done. The
§Verification conduct rules address whoever makes the change. That is a person or an agent,
and the rules do not distinguish.

Every rule is portable. A single-crate CLI or a small Python tool with no CI runner can
comply. The minimum is:
- tests that were shown to fail before the fix;
- one local check script that CI, when it exists, also runs;
- no test retries;
- a handoff that lists each command and its outcome.

Orbit is the worked example, not the audience. Orbit paths are relative to
`codebases/orbit/`.

Other standards own the neighbouring rules, and this one does not repeat them:
- **Test layout and behaviour-only assertions in Rust** are
  [STD-02](STD-02-rust-architecture-and-errors.md) §R19 to §R21. §R5 below extends the
  behaviour-only rule to every language.
- **Test waits, fixture process guards, the self-re-exec ban and host-state isolation** are
  [STD-03](STD-03-concurrency-and-process-safety.md) §R17 to §R20. Runtime containment and
  wall-clock limits for test runs are STD-03 §R21 and §R22.
- **Help text and golden-tested CLI surfaces** are [STD-01](STD-01-cli-surface.md) §R22 and
  §R24.
- **Security boundaries** are STD-05-security-boundaries. Only the documented claim about a
  boundary (§R13) is in scope here.

The keywords MUST, MUST NOT and SHOULD are normative. A rule marked *review-only* in
§Machine checks has no automated gate, so a reviewer enforces it.

## Rules

### Test design

- **R1.** A regression test MUST drive the entry point that production callers use: the
  same surface (CLI, API handler, tool dispatch), the same session or authority shape, and
  the same input shape a real caller sends. It builds the hostile state directly rather than
  approximating it.
- **R2.** A regression test MUST be shown to fail against the unfixed code before the fix is
  accepted. The failing run is part of the change's evidence.
- **R3.** When a correct guard or test blocks a change, the change MUST fix the upstream
  defect. It MUST NOT relax the guard, delete or loosen the assertion, or widen the
  accepting path to get past it.
- **R4.** A change that deliberately widens a fail-closed check MUST first run that check's
  existing negative cases, and they MUST still fail after the change. A negative case exists
  for every guarantee the check makes, not only the one next to the reported bug.
- **R5.** In every language, a test MUST assert behaviour by running the code under test.
  STD-02 §R21 states the rule for Rust. Two exceptions are allowed:
  - A text-matching test over source, assets or UI copy is allowed only as a narrow
    structural safety guard, and its assertion message names what it protects.
  - A test that pins operational policy owned by config or prompts (agent crew or model,
    schedule, prompt or UI wording) is allowed only if its assertion message cites the
    incident it guards.

### Hermeticity and isolation

- **R6.** A test MUST NOT change process-global state in the shared test process unless
  every test in that process that can observe the state is serialized with it, readers
  included. Process-global state covers the environment, `PATH`, the current directory,
  statics and global overrides. Otherwise the test runs the code in a child process with the
  value set on the child command. This extends STD-03 §R20: a lock that only writers take is
  not isolation.
- **R7.** A test MUST NOT depend on a live service, the host's service manager, the network,
  or discovery that walks above its temporary root, such as VCS or config-file search. The
  fixture injects the dependency or sets the boundary itself. STD-03 §R20 covers the host's
  own state directories.
- **R8.** A test that cannot run in the current environment MUST skip visibly: its output
  names the missing capability. At least one CI job, or the release checklist where there is
  no CI, MUST run it where the capability exists, in a mode where a skip fails.
- **R9.** A flaky test MUST be fixed at its root cause. Retries, sleeps and artificial
  throttles MUST NOT be added to make it pass. While the fix is pending, the test MAY be
  quarantined: marked ignored, with a reason that names the tracking issue.

### Gates and CI

- **R10.** A gate that ran zero tests or asserted nothing MUST fail. Every test selection in
  a gate (a filter, a named group, an ignored-only leg) MUST select at least one test. A tool
  the gate depends on MUST be present, or the gate fails. A missing tool never degrades to
  empty, "clean" output.
- **R11.** Generated and derived artifacts MUST be regenerated in the same change as their
  source. These include goldens, snapshots, mirrored or vendored copies, generated indexes
  and lockfiles.
- **R12.** The fast local gate an author runs before handoff MUST include every parity and
  drift check that CI runs: the cheap `--check` modes of generators, mirrors and indexes.
  Only checks that need a full build MAY be CI-only.

### Docs as contracts

- **R13.** Comments, help text, documentation and validation stamps are claims, and a claim
  the code does not uphold is a defect. The change that breaks a claim MUST fix the code or
  the claim, and it MUST NOT restamp a claim as validated without validating it. This binds
  hardest on security guarantees.
- **R14.** Every documented command and code example MUST run against the current version.
  A documented rule or procedure that points at a removed command MUST be deleted or
  rewritten in the same change that removes the command.

### Verification conduct

- **R15.** A validation step counts only with positive evidence that it completed and
  passed: the producer's own exit status, or an explicit completion marker. A step that was
  skipped, backgrounded, denied by a sandbox, or whose status was hidden behind a pipe is not
  a pass.
- **R16.** A handoff MUST list each validation command with its outcome: passed, failed, or
  not run with the reason. "Tested", "all green" or "CI passes" without the commands is not
  a report.
- **R17.** Before treating a red gate as caused by the change, or as a blocker, the author
  MUST reproduce it on a pristine tree at the base commit. A failure that reproduces there is
  pre-existing. The author records it and hands it off separately, and does not fold the
  unrelated fix into the change.
- **R18.** Before acting on a task, issue or finding, the author MUST verify its premises
  against the live code at the current head. That includes checking whether the work already
  landed.
- **R19.** After a write to durable or shared state (a tracker record, a remote, a shared
  file), the author MUST read the result back and confirm it took effect. Nothing, including
  a display filter such as `| head`, may hide the write's result.
- **R20.** Before retrying a write whose result is unknown, the author MUST list or query
  for the record the write would have created, so a retry cannot create a duplicate.
- **R21.** Before a destructive operation on shared state, the author MUST capture what it
  destroys in a recoverable form: a commit SHA, a copy, an export, or a dry-run inventory.
- **R22.** The author MUST NOT modify, stash, clean or discard another actor's work. That
  work includes another session's uncommitted edits, pre-existing changes in a checkout, and
  another person's branch. Use operations scoped to the author's own changes.

## Why

The Orbit evidence below is cited by task (`ORB-`), friction (`F`) and decision-log line.
`D:<area>:<n>` means `codebases/orbit/docs/design/<area>/4_decisions.md` line n. The
candidates were mined and dispositioned in ORB-13088
(`projects/standards/tacit-extraction-2026-09-26.md`).

**R1.** In ORB-12582, the distributed drain's read-only surface refused every `orbit tool run`
call, including the owner's own, while MCP served the same calls normally. The unit test that
should have caught it synthesized a capability set for its "owner-local session", which is "a
session no production CLI caller sends" (D:operations-as-data:271). A test that builds its own
convenient entry state proves only that the convenient state works. Other cases:
- ORB-11021: a classifier was "never enforced at MCP dispatch".
- ORB-12791 and ORB-12149.
- F2026-08-005: a feature shipped with no reachable caller.
- F2026-08-139.

In a CLI, the production entry point is the built binary. STD-02 §R20 is where those tests
live in Rust.

**R2.** D:activity-job:1071: "A fixture hand-built with a singular `task_id` passes against
the broken code and proves nothing." Only a failing run on the pre-fix code shows that the
test can see the defect at all. The evidence is the same as R1: ORB-12582, ORB-11021,
ORB-12791, ORB-12149, F2026-08-005 and F2026-08-139.

**R3.** D:activity-job:784 rejects weakening `pipeline_success_guard`: "The guard behaved
correctly on false input; the input was the defect." D:activity-job:1495 rejects relaxing a
guard on the local path because it "deletes the evidence rather than producing it." A loosened
assertion turns a signal into silence and leaves the defect upstream. See also ORB-12297 and
ORB-12539. Orbit's development guide states the test-side form: "Never weaken the assertion
taken when the probe *is* available."

**R4.** F2026-07-179 was a near-miss safety regression. A task's suggested fix widened a
fail-closed worktree guard to accept any change disjoint from the run's own paths. Implemented
literally, it turned five provider-escape tests green that must stay red, because every escape
was also disjoint. The failing negative cases were the only thing that surfaced it. The
friction's guardrail is the rule: "these existing fail-closed cases stay red", enumerated for
all of the guard's guarantees.

**R5.** A text-matching test breaks on every wording edit and proves nothing runs. A test
that pins crew, model or schedule turns every operations edit into red CI. Orbit's agent guide
bans both and allows the narrow structural-guard exception (`codebases/orbit/AGENTS.md` lines
13 and 31; `docs/design-patterns/test_layout.md` §What tests assert). STD-02 §R21 carries this
for Rust only. This rule makes it universal, because the failure mode is the same in any
language.

**R6.** ORB-12123: `GitShim` mutated the process-wide `PATH`, which redirected `git` for
every concurrent test in the same binary. Those tests then failed once the shim's temp
directory was removed. F2026-09-107 was a flake at about 50%. See also ORB-12479. A mutex that
only writers take still lets a reader see a half-applied change. Running the mutation in a
child process removes the shared state entirely. F2026-07-065 is the host-facing version:
"validation must never rewrite the operator account['s] live skill links."

**R7.** Evidence for R7 to R9 is shared, from candidate X4:
- F2026-09-148: tests queried the real per-user systemd manager, and they failed with HTTP 500
  on a worker without a user bus. Orbit fixed it by injecting a clock observer into the
  fixture.
- F2026-09-215: non-Git fixtures had no Git discovery boundary. With `TMPDIR` inside a
  managed worktree, `git rev-parse` walked upward and found the managed checkout, and 33 of
  80 tests failed on a read-only lock there.
- ORB-11390, ORB-10835, ORB-12532, ORB-12306.

A test whose result depends on the host is a report on the host, not on the code.

**R8.** ORB-11390: a test "hard-fails instead of skipping in restricted" environments.
Failing for a reason unrelated to the code under test teaches people to ignore red. The
opposite fault is just as bad. A silent early return reads as a pass. Orbit's Linux CI
sandbox gate exists "instead of reporting those tests as passed after an early return". An
ignored test with a reason, plus a named job that runs it with `--run-ignored only`, gives
both properties.

**R9.** ORB-12306 is titled "Remove artificial SQLite write admission": it removed an
artificial throttle. A retry that makes a flaky test pass also makes a real
intermittent defect pass. The remaining evidence is shared with R7.

**R10.** Evidence:
- ORB-11609: "two of its test steps run zero tests."
- ORB-12130: a gate "passes vacuously when its hand-written test-name literal goes stale."
- ORB-12452: a gate was inert because an action was pinned to a commit that does not exist.
- F2026-08-083: a gate "reports PASS while asserting nothing."
- F2026-08-108: test files never declared in their module compiled to nothing and reported
  `0 passed`.

`cargo test` with a filter that matches nothing exits 0. That is the default hazard this rule
removes.

**R11.** Evidence: ORB-12242, ORB-12221, ORB-12589 and F2026-07-185. ORB-11402 is the
incident: the integration branch went red on the sync gate. A source change without its
regenerated artifact is half a change, and it turns the next author's gate red.

**R12.** Evidence is shared with R11. If a parity check runs only in CI, drift is found after
the push, by someone else, on a shared branch. The `--check` modes cost seconds, so the fast
gate can afford every one of them.

**R13.** Evidence:
- ORB-12775: a documented sandbox guarantee did not hold. `ORBIT_ALLOWED_TOOLS` was enforced
  through an environment variable that the sandboxed child could unset.
- ORB-12183: stale evidence-rule descriptions.
- ORB-12046: "Restore honest validation dates."
- ORB-12055: a doc was stamped validated while it still carried a broken path.

Readers act on a claim without reading the code. A false security claim is worse than no
claim, because it removes the reason to check.

**R14.** Evidence:
- ORB-12673: "the documented `backlog` retry" is unreachable.
- ORB-12581.
- ORB-12043: the "mutable-fixture template does not compile."
- F2026-07-108: a rule that points at a removed command "is worse than no rule."

The candidate recorded more than 11 such cases. Documented commands are the ones agents
copy verbatim.

**R15.** F2026-09-077: a build was started in the background as
`cargo build … 2>&1 | tail -40`, and the pipeline's status was `tail`'s, not the build's.
See also F2026-09-127, ORB-12527 and ORB-12548 (QA validation tasks). A check that never
finished, or whose failure was filtered away, looks exactly like a pass in a summary.
Orbit's review gate records `denied` and `not_run` as distinct outcomes for this reason. It
also refuses a claim whose outcome contradicts its role.

**R16.** Orbit's agent guide requires it: "Report commands and outcomes at handoff — passed,
failed, not run — never 'tested'" (`codebases/orbit/AGENTS.md` line 35). A reviewer can
re-run a listed command and can see what was not run. "Tested" offers neither.

**R17.** The candidate recorded about 20 records. Evidence:
- F2026-09-036: both gates failed on untouched code.
- F2026-08-084: "`make ci-fast` was already red on HEAD."
- F2026-07-129: the author isolated the cause by "spinning a temporary git worktree at the
  branch point". The failure came from an earlier merge that left a snapshot stale.

Without a baseline, an author either blocks on someone else's breakage or folds an unrelated
fix into an unreviewable diff.

**R18.** The candidate recorded 13 or more records. Evidence:
- F2026-08-053: the dated root cause no longer existed.
- F2026-09-092: the defect was already remediated.
- F2026-09-213.

Orbit's review sweep states the rule: "Verify every finding against the live code before
filing" (`crates/orbit-core/assets/auto_tasks/code-review.yaml` line 72). Task text ages. The
code at head is the only authority.

**R19.** F2026-09-158: `orbit.task.add … | head -c 800` truncated the JSON before the `id`
field, so the created task looked like a failure. A write whose result is not read back
cannot be told apart from one that silently half-applied.

**R20.** In F2026-09-158, re-running the same command created a second identical task
(ORB-12450 and ORB-12451). The agent surface could not delete the duplicate. F2026-08-002 was
a duplicate friction from an accidental repeat call. A retry must first look for the record
the retry would create.

**R21.** Evidence is shared with R22: F2026-07-142 and F2026-07-160. Capture is what makes a
mistaken destructive step recoverable. Orbit's implement activity records the starting HEAD
and a full `git status --short` inventory before any write, for this reason.

**R22.** F2026-07-142: `refs/stash` is shared across linked worktrees. A failed
`git stash push -- <path>` made the next `git stash pop` restore an unrelated session's work
into the wrong tree. See also F2026-07-160. Shared repositories, trackers and hosts have
other actors, and an operation that is not scoped to one's own changes eventually lands on
theirs.

## Exemplars

Paths are relative to `codebases/orbit/`.

- R1 — `crates/orbit-cli/tests/mcp_roundtrip.rs#readonly_state_mount_keeps_cli_and_mcp_reads_observational`
  (drives "the production CLI and MCP entry points" inside a real read-only mount, "rather
  than approximating the boundary with permission bits");
  `docs/design/operations-as-data/4_decisions.md` (the ORB-12582 entry, around line 271).
- R2 — `crates/orbit-types/src/workflow/review.rs#ValidationRole` (`ExpectedFailure`: "the
  pre-fix reproduction … The failure is the positive evidence");
  `crates/orbit-core/src/application/review/gate/tests/judgement.rs#classifications_that_contradict_their_outcome_or_explain_nothing_are_refused`;
  `scripts/cross-revision-check.sh` (the same command on two revisions).
- R3 — `docs/design/activity-job/4_decisions.md#Derive the delivery execution summary from the change, not from the agent`
  (Rejected alternatives); `docs/DEVELOPMENT.md#Tests that depend on host process visibility`
  ("Never weaken the assertion").
- R4 — `crates/orbit-engine/src/activity_job/cli_runner/tests/orchestrator_worktree.rs`
  (widened acceptance cases such as
  `concurrent_primary_fast_forward_does_not_block_disjoint_worktree_changes` beside the
  `expect_err("… must remain fail closed")` cases);
  `crates/orbit-policy/src/tests/matrix.rs` (a table-driven allow/deny matrix that records
  surprising behaviour "instead of silently changing enforcement semantics").
- R5 — `AGENTS.md#Rules` (the text-matching ban and its structural-guard exception) and
  `#Code` (invariants, not policy);
  `docs/design-patterns/test_layout.md#What tests assert (invariants vs. policy)`.
- R6 — `crates/orbit-engine/src/activity_job/tests/workspace.rs#GitShim` (`install`
  re-runs the test in a child with `PATH` set on the child command);
  `crates/orbit-common/src/test_env.rs#ScopedEnv`; `.config/nextest.toml` (the
  `stateful-runtime` group and its comment about keeping filters narrow).
- R7 — `crates/orbit-web/src/api/tests/routines.rs#with_clock_status` (an injected clock
  observer instead of the live user manager); `scripts/cross-revision-check.sh`
  (`GIT_CEILING_DIRECTORIES=$WORKDIR`).
- R8 — `crates/orbit-tools/src/plugin/tests/mcp.rs`
  (`#[ignore = "requires a host plugin sandbox; the Linux CI sandbox gate runs it"]`);
  `.github/workflows/ci.yml` (the `Linux sandbox/policy enforcement gate` step,
  `--run-ignored only`); `crates/orbit-common/src/test_env.rs#start_identity_probe_blocker`
  (a skip that names its constraint).
- R9 — `.config/nextest.toml` (no `retries` configured); `docs/DEVELOPMENT.md#Tests that depend on host process visibility`
  (assert the fail-safe branch instead of failing for an unrelated reason).
- R10 — `scripts/check-ci-macos.sh` ("filtered cargo test matched zero tests");
  `scripts/ci-guardrails.sh` (fails fast when `rg` is missing, because "empty search results
  read as clean" [ORB-10021]); `scripts/check-orphan-modules.sh` (F2026-08-108);
  `scripts/check-workflow-action-pins.sh` (ORB-12452).
- R11 — `Makefile#goldens` and `scripts/check-goldens.sh` (verify, or regenerate with
  `UPDATE=1`); `AGENTS.md#Gates`.
- R12 — `Makefile#ci-fast` (`ci-guardrails.sh --fast`); `scripts/ci-guardrails.sh` (every
  `--check` parity script, such as `generate-doc-indexes.sh --check` and
  `sync-plugin-skills.sh --check`, runs in both modes; fast mode skips only the workflow test
  listing, clippy, tests, doctests, rustdoc and cargo-deny).
- R13 — `docs/design/policy-sandbox/1_overview.md#2.5 Exec supervision is not default OS isolation`
  (states what is *not* guaranteed); `AGENTS.md#Rules` ("Stale docs are a review blocker").
- R14 — `scripts/ci-guardrails.sh` (`cargo test --no-fail-fast --workspace --doc`);
  `scripts/check-crate-agent-guides.sh` (every relative link in a crate guide resolves).
- R15 — `crates/orbit-types/src/workflow/review.rs#ValidationOutcome` (`passed`, `failed`,
  `denied`, `not_run`); `scripts/cross-revision-check.sh` (status captured "from a redirect
  before any bounded display", F2026-09-077); `.github/workflows/ci.yml` ("if this step is
  missing, the gate did not pass").
- R16 — `AGENTS.md#Code` (the last bullet);
  `crates/orbit-core/src/application/review/gate/tests/judgement.rs#inconsistent_claims_and_denied_validation_are_downgraded_honestly`.
- R17 — `crates/orbit-core/assets/activities/agent_implement.yaml` (validation item 7:
  "reproduce it once on the unmodified baseline"); `scripts/cross-revision-check.sh`.
- R18 — `crates/orbit-core/assets/auto_tasks/code-review.yaml` (§File confirmed findings,
  line 72); `crates/orbit-core/assets/skills/orbit/references/task-execution.md#Step 4 — Implement and validate`
  (already-landed work needs covering evidence); `AGENTS.md#Rules` ("Judge from current code,
  tests, and requirements").
- R19 — `crates/orbit-core/assets/activities/agent_implement.yaml` (item 11: re-read the
  durable `context_files` after any update, and take a final inventory).
- R20 — review-only in Orbit; F2026-08-002 and F2026-09-158 are the counter-examples.
- R21 — `crates/orbit-core/assets/activities/agent_implement.yaml` (item 3: record the starting
  HEAD and a full starting inventory); `docs/design-patterns/command.md#Destructive CLI confirmation`
  (bulk cleanup defaults to a dry run).
- R22 — `crates/orbit-core/assets/skills/orbit/references/task-execution.md#Step 4 — Implement and validate`
  ("never use positional `git stash` / `git stash pop`");
  `crates/orbit-core/assets/activities/agent_implement.yaml` (item 10: "Preserve pre-existing
  contents, another actor's edits").

## Machine checks

Each gate below is real and can be adopted. "Orbit:" says whether Orbit enforces it today.

| Rule | Gate |
|---|---|
| R1 | review-only. In Rust, put end-to-end tests in crate-root `tests/` against the built binary (STD-02 §R20). |
| R2 | Before/after run: extract the base revision into its own directory (`git archive <base> \| tar -x -C <dir>`), or use a separate `git worktree`. Add only the new test, and record its failing run. Orbit: `scripts/cross-revision-check.sh`, and the review gate refuses a negative control recorded as `ExpectedFailure` that passed (`judgement.rs`). |
| R3 | review-only. A diff that deletes or relaxes an assertion or guard must say why the guard was wrong. |
| R4 | review-only. The widened check's negative tests appear in the validation record as run and failing where they should. |
| R5 | review-only (as STD-02 §R21). An optional CI grep can flag `include_str!` plus `.contains(`, or reads of source files in tests, against an allow-list of named structural guards. Orbit: no such grep. |
| R6 | nextest `[test-groups]` with `max-threads = 1` for every module that mutates or reads the mutated state (Orbit: `stateful-runtime`). nextest's process-per-test model bounds the blast radius. Python: pytest `monkeypatch` plus `env=` on child processes. Readers left outside the group: review-only. |
| R7 | review-only. Optionally run the suite with no network (`unshare -rn`, or a CI job with networking disabled). Fixtures set `GIT_CEILING_DIRECTORIES` to their temp root. Orbit: no network-off job. |
| R8 | Rust: `#[ignore = "<capability>"]`, plus a CI step in a capable job running `cargo nextest run --run-ignored only -E '<filter>'`, or `cargo test -- --include-ignored`. Run `--include-ignored` only in jobs that have the capability, never as the default. Python: `pytest -rs` prints skip reasons, and a `REQUIRE_<CAPABILITY>=1` variable set in the capable job turns the skip into a failure. Orbit: the Linux sandbox/policy enforcement gate. |
| R9 | nextest: leave `retries` unset (0) in `.config/nextest.toml` and off the CI command line. pytest: no `--reruns` or `pytest-rerunfailures` in CI. Orbit: no retries configured. |
| R10 | nextest: `--no-tests=fail` (or `NEXTEST_NO_TESTS=fail`). Set it explicitly, because the `auto` default resolves to `fail` only on recent nextest. pytest exits 5 when nothing is collected; never mask that code. A per-filter count check fails any selection that matches zero tests. Every required tool is checked for presence at gate start. Orbit: `check-ci-macos.sh` per-filter counts, the `rg` presence check, the orphan-module check. The nextest flag is not set, so Orbit relies on the pinned version's default. |
| R11 | CI regenerates and diffs: `<generator> && git diff --exit-code`, or a `--check` mode. Orbit: `make goldens` (`check-goldens.sh`), `generate-doc-indexes.sh --check`, `sync-plugin-skills.sh --check`. |
| R12 | The fast gate calls the same check-mode commands as CI, from one script. Orbit: `make ci-fast` runs `ci-guardrails.sh --fast`, which differs from CI only by the compile-heavy steps. Adding a new CI parity check to the fast gate: review-only. |
| R13 | review-only. A testable claim, and every security claim, gets a test that exercises it. Help-text changes surface in goldens (STD-01 §R24). |
| R14 | Rust: `cargo test --doc` (Orbit: in `ci-guardrails.sh`). A link checker for relative doc links (Orbit: `check-crate-agent-guides.sh`; polaris: `scripts/check-links.sh`). Documented CLI command lines can be parsed through the real argument parser in a test. Orbit: no general extraction of documented commands. Otherwise review-only. |
| R15 | `set -o pipefail`, or capture `$?` from a redirect before any `tail`/`head`. A structured validation record whose outcomes are `passed`/`failed`/`denied`/`not_run`. Orbit: the review gate's `ValidationOutcome` and contradiction check. Otherwise review-only. |
| R16 | A handoff or PR template with a commands-and-outcomes section. Orbit: review reports carry each validation command with its `ValidationOutcome`. Otherwise review-only. |
| R17 | review-only. Orbit: `scripts/cross-revision-check.sh` runs one command on the base and the candidate. |
| R18 | review-only. Orbit's pipeline requires covering evidence (`already-landed.json` with validation logs) before it accepts an already-landed claim. |
| R19 | review-only |
| R20 | review-only. Where the store supports it, an idempotency key makes a repeated write safe. |
| R21 | review-only. Destructive commands default to a dry run and require `--confirm` (STD-01 §R5). |
| R22 | review-only. Orbit mounts `.git` read-only in managed runs, which prevents ref-level interference such as a shared stash. |

## Applies to

- `universal`: R1 to R22. Any project with tests, CI or documentation, of any size or
  language, and any author of a change, human or agent.
- `rust`: the nextest, `#[ignore]`, `cargo test --doc` and `--include-ignored` gates in
  §Machine checks. Rust projects also follow STD-02 §R19 to §R21 for test layout.
- `cli`: R13 and R14 bind on help text and documented command lines, alongside STD-01 §R22
  and §R24.
- `stateful`: R19 to R22 bind hardest where the state is shared: trackers, remotes and
  repositories with more than one actor.

## Deviations

An adopting repo that departs from a rule records a decision in its own design docs, citing
`STD-04@<version> §Rn` and the reason, for example `STD-04@1 §R8`. It never edits its
vendored copy of this standard.

Some deviations are expected, and the decision still needs to be recorded:
- A project with no runner that has a capability (for example no Linux host with user
  namespaces) cites `STD-04@1 §R8`. It names the manual release-checklist step that runs the
  ignored tests.
- A parity check that needs a full build may stay CI-only under `STD-04@1 §R12`, and the
  decision names it.

Known deviations in the exemplar itself, so adopters do not copy them:
- `crates/orbit-cli/tests/mcp_roundtrip.rs#readonly_state_mount_keeps_cli_and_mcp_reads_observational`
  returns early, printing nothing, when bubblewrap mount namespaces are unavailable. No CI mode
  fails that skip (against R8).
- `scripts/check-workflow-action-pins.sh` warns and exits 0 with no network, or when
  rate-limited, and it makes no exception for CI (against R10).

## Changelog

- v1 (2026-09-26): initial, sources ORB-13088.
