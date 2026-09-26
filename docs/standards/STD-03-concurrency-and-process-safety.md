---
id: STD-03-concurrency-and-process-safety
title: Concurrency and process safety — locks, channels, durable writes, state evolution, subprocess lifecycles, destructive operations, remote effects, test containment and resource bounds
summary: Normative rules for async locking, bounded queues and waits, cleanup guards, atomic and locked durable writes, version skew and append-only migrations, subprocess supervision and process identity, pinned inputs and interference-scoped guards, ownership-proven destruction, outcome-unknown remote calls, test-fixture safety and resource ceilings; follow it in any project that runs concurrent code, spawns processes, talks to peers, or persists state.
status: active
tags: [standard, concurrency, subprocess, testing, durability, migrations, git, rust, python]
version: 2
created: 2026-09-26
updated: 2026-09-26
last_validated: 2026-09-26
related: [STD-01-cli-surface, STD-02-rust-architecture-and-errors, STD-04-testing-and-verification, STD-05-security-boundaries]
---

# Concurrency and process safety

These rules cover shared state, async locking, durable writes and how that state evolves, child
processes, destructive and remote operations, and the tests that exercise them. Most of them
come from failures the Orbit codebase has already had. Two of those failures took down the host
that runs the constellation: the 2026-09-06 fork storm and the 2026-09-23 out-of-memory outage.
The principles hold in any language. Rust specifics (lints, `Drop`, `std::process`) and Python
equivalents appear where they apply. Orbit is the worked example here, not the audience. Orbit
paths are relative to `codebases/orbit/`.

The keywords MUST, MUST NOT and SHOULD are normative. A rule marked *review-only* in
§Machine checks has no automated gate, so a reviewer enforces it. Rule numbers are stable: v2
added R23 to R32 inside their clusters, so numbers do not run in order down the page.

## Rules

### Async and in-memory shared state

- **R1.** A synchronous lock guard (a mutex, a read-write lock, or a `RefCell` borrow) MUST NOT
  be held across an `.await` or any other suspension point. Copy the data out or end the
  guard's scope first. Where the critical section itself must await, use an async-aware lock.
  A lock of either kind, or an open database transaction, MUST NOT be held across slow work: a
  subprocess, network I/O, model inference, or a scan of a large file or directory. The one
  exception is a lock whose only job is to serialize that work, such as a per-key single-flight
  gate that nothing else waits on. Blocking calls MUST NOT run on an async executor thread;
  hand them to a blocking pool.
- **R2.** Every channel or queue between concurrent tasks or threads MUST be bounded. The
  construction site MUST state what a producer does when the queue is full: wait, which applies
  backpressure; drop with a counted signal; or refuse the request.
- **R3.** Code that holds more than one lock at a time MUST acquire the locks in one documented
  order, the same order on every path.

### Cleanup guards

- **R4.** Every side effect that must be undone MUST be bound to a guard. Examples are a held
  lock, a temp file, a changed signal disposition, an environment override, a spawned child, or
  a pending-write marker. The guard's destructor or `finally` block runs on success, error
  return, panic or exception, and cancellation or timeout. It defaults to the failure or
  rollback outcome, and it never panics or throws.

### Durable on-disk state

- **R5.** A durable file MUST be replaced atomically. Write a temp file in the same directory,
  flush it (`fsync`), rename it over the target, and `fsync` the parent directory when the write
  must survive a crash. A durable file is never truncated and rewritten in place. Every durable
  write in a codebase goes through one shared helper, so the flush and rename steps cannot drift
  between copies. A record that spans several files is staged complete in a temp directory
  beside its final location and renamed into place, so no reader sees a partial record.
- **R6.** On-disk state that more than one process mutates MUST be changed only while holding
  an OS advisory lock that the kernel releases when the holder dies: `flock`, `fcntl` or
  `LockFileEx`. Open the lock file close-on-exec. Never use a create-if-absent lock file or a
  PID file as the lock. A read-modify-write of shared durable state (a registry, a config file,
  a schema) MUST hold the owning lock from before the read until after the write, or MUST
  commit with a compare-and-set against the version it read. An atomic load followed by an
  atomic save is not an atomic update.
- **R7.** Lock acquisition MUST be bounded by a deadline. A timeout MUST report who holds the
  lock: the holder records its PID, the time it acquired the lock, and a label. The holder
  record is diagnostic only. The holder writes it just after acquiring, so a waiter can briefly
  read the previous holder's record, or none, and nothing decides ownership from it.
- **R8.** Writes that must become visible together across more than one store MUST publish
  through one durable commit decision: a single transaction or journal record. Every other
  effect replays idempotently from that decision. Never chain independently committed writes.
- **R9.** Recovery of an interrupted multi-step write MUST finish before any state is exposed:
  it either completes the write or refuses and keeps the store closed. It MUST NOT drop an
  obligation that was already decided, and it MUST NOT guess the outcome from age or a timeout.

### Version skew and state evolution

- **R10.** A binary that finds persisted state newer than it understands MUST NOT write to it,
  migrate it down, or reinterpret it. The binary has two options:
  - If the newer writer declared the changes compatible, open the state read-only, with writes
    refused at the storage layer.
  - Otherwise, refuse to open it, and name the first incompatible migration it lacks.

  A writer declares a migration compatible only when binaries that lack it still read the state
  correctly. A change that removes, renames or reinterprets state that older binaries touch is
  incompatible, and so is any change whose compatibility is in doubt.
- **R23.** Every schema or on-disk layout change MUST ship as a new migration appended to an
  ordered, versioned registry. A shipped migration is never edited, reordered or removed, and
  the baseline schema is frozen once released. Each migration applies in one transaction
  together with its ledger record. A store created fresh and a store upgraded from the oldest
  supported version MUST end with the same schema.
- **R24.** A new or stricter validation rule MUST migrate or grandfather existing state that
  the old rule accepted: a config file, a persisted record, an identifier an earlier release
  minted. An old format stays readable even when it is no longer written. Where neither is
  possible, the refusal names the file or record and a repair that works without the tool,
  because a tool that refuses to load the state cannot be used to fix it.
- **R25.** *(SHOULD)* A tool that installs default resources (config, templates, job
  definitions) into user-owned locations SHOULD reconcile them by provenance. It records a
  digest of what it last wrote, and it refreshes or retires a file only while the file's bytes
  still match that digest. A user-authored or user-modified file is preserved; a modified file
  that a release retires is moved aside, not deleted. Re-running init without a force flag
  SHOULD NOT reset a setting the user changed.
- **R26.** *(SHOULD)* A fix to persisted state, installed resources or runtime behaviour SHOULD
  reach installs that already exist and processes that are already running, not only a fresh
  init. The upgrade path runs the *new* binary's migrations and resource reconciliation against
  existing state. Long-lived processes are drained and restarted onto the new binary, or they
  stay visibly marked as running the superseded one. Replacing the binary on disk is not a
  deploy.

### Subprocesses

- **R11.** Every spawned child MUST be the leader of its own process group, or be placed in its
  own cgroup or job object, so that the child and everything it spawns can be signalled as one
  unit.
- **R12.** Termination on timeout, cancellation or a parent signal MUST follow three steps. Send
  a polite signal (SIGTERM) to the whole group. Wait a bounded grace period. SIGKILL the group
  if anything survives. Survival is decided by probing the group itself, and a probe that cannot
  answer counts as survival. It is never inferred from the lead child's exit.
- **R13.** Every exit path, including a clean exit, MUST end with the child reaped and its group
  swept before the supervisor returns. Sweeping means SIGKILL to any surviving member. The
  supervisor reports how the child actually ended: its exit code, or the signal or timeout that
  ended it. A child that failed to start, timed out or was killed is never reported as a clean
  exit.
- **R14.** A supervisor MUST NOT signal a PID or process group after the child that owned it has
  been reaped. It signals as a group only a live group leader that it spawned itself. Any
  persisted or cross-process reference to a process MUST identify it by PID together with its
  start identity (a start-time token, plus the PID namespace where that can differ), never by a
  bare PID. A live PID with a different start identity reads as exited. A probe that cannot
  answer reads as unknown. Unknown is never proof that the process is gone, and never proof of
  identity where the answer grants authority.
- **R15.** A child's stdout and stderr MUST be drained at the same time as the wait, and
  captured output MUST have a byte limit. When the limit is reached, the child is terminated or
  its output truncated. The same limit applies to captured text passed onward, such as an error
  message or a peer's reply embedded in a record or a prompt: it is bounded, the truncation is
  marked, and the full text is kept somewhere durable.
- **R16.** A deliberately detached process MUST satisfy two conditions: a durable owner record
  identifies it, and it runs in its own resource-bounded container (a cgroup scope, unit or job
  object). A detached process is one started with `setsid` or daemonized, so it outlives its
  spawner.

### Tests and fixtures

- **R17.** A test that waits for a concurrent process MUST wait in-process, with an explicit
  deadline and a liveness check on the child. It MUST NOT use a shell loop that forks a process
  on every iteration, and MUST NOT wait without a bound.
- **R18.** Every test fixture that spawns a process MUST own it through a panic-safe guard that
  terminates and reaps it on drop.
- **R19.** Tests MUST NOT run the real product binary or worker through a production
  self-re-exec path (`current_exe()`, `sys.argv[0]`). The spawn path requires an explicit test
  substitute. It also refuses at runtime when the executable is a test harness, whatever
  compile-time test flags are set.
- **R20.** Tests MUST NOT read or write real host state. Every fixture runs against temporary
  roots. `HOME` and every product-specific root or authority variable are set or cleared on the
  child command, not only in a parent shell. Tests that change process-global state (the
  environment, the current directory) are serialized under one process-wide guard or test
  group.

### Containment and resource bounds

- **R21.** The runtime that hosts test or agent workloads MUST reclaim every descendant when
  the owning unit ends, whether or not in-process cleanup ran. That runtime is the CI job, the
  sandbox, or the pipeline run. Fixture cleanup and runtime containment are separate safety
  layers, and neither may rely on the other.
- **R22.** Anything that runs unattended MUST have a wall-clock timeout and a process-count and
  memory ceiling, enforced outside the process itself. That covers services, timer jobs,
  workers, CI jobs and test runs. Inside the process, every wait on a peer, a child, a lock or a
  stream MUST also have a deadline, including joins on output readers after a timeout. A
  streaming read or long transfer uses an idle deadline that resets on progress, not a single
  deadline for the whole request, so a slow live transfer finishes and a stalled one ends. An
  operation longer than its caller can wait SHOULD return a handle to poll instead of blocking
  the caller.

### Moving inputs and integrity guards

- **R27.** A long-running operation MUST resolve each moving reference it depends on (a branch
  name, a `latest` tag, a symlink, a pointer in config) to an immutable identifier once, at
  start. It records that identifier and passes it to every later step, and no later step
  re-resolves the name. An input that the operation's contract allows to change while it runs,
  such as a scope the operation may widen, is re-read at verify time rather than frozen at
  start.
- **R28.** A fail-closed concurrency or integrity guard MUST compare only state that can affect
  the operation it protects, such as overlap with the operation's own paths or records. It MUST
  NOT demand that unrelated shared state stay byte-identical, because then every unrelated
  concurrent write fails the guard. A change the guard cannot classify still fails closed, and
  a guard failure preserves the work it stopped (R30).

### Destructive operations and recovery

- **R29.** Before deleting, resetting, overwriting or reclaiming anything, code MUST establish
  positively that it owns the target and that the target holds nothing unrecoverable. Ownership
  comes from recorded provenance: a digest of what this code wrote, an owner record, or a
  registration this attempt created. It never comes from a location, a name, or the absence of
  another claim. A target that is unreachable, unreadable, ambiguous or only partly read is
  kept and reported, never deleted. On timeout or failure, code mutates only state that this
  attempt owns.
- **R30.** A failure path MUST make completed work durable and recoverable before any teardown.
  It commits or snapshots the work and records where it is, and only then removes the worktree,
  temp directory or sandbox. The preservation step runs before the fallible operations (a push,
  a network call, a gate) that might end the run, not after them.

### Distributed and remote effects

- **R31.** Once a request that can change remote state has been sent, a lost or timed-out reply
  MUST be reported as outcome unknown: not as failure, and not as the destination being
  unreachable. A retry MUST carry an idempotency key or request id so the destination can
  recognize the repeat. A silent or unreachable peer is not a dead one: work it holds is never
  reclaimed automatically because a TTL or heartbeat lapsed. Age supports inspection and an
  explicit, recorded reclaim.
- **R32.** Git automation MUST fetch the relevant refs before judging containment, ancestry or
  freshness against a remote. It MUST update a shared remote ref with an expected-old-value
  compare-and-set, and it merges reviewed work only at the reviewed head commit. A ref that
  moved is a conflict to surface. It is never merged, replayed or force-pushed over
  automatically.

## Why

This section gives the reasoning and incident for each rule. The two Orbit incident write-ups
are `docs/rca/2026-09-06-recursive-test-worker-fork-storm.md` (the **09-06 fork storm**) and
`docs/rca/2026-09-23-cross-crate-test-worker-oom.md` (the **09-23 OOM**). The containment
procedure is [RB-12](../runbooks/RB-12-cpu-fork-storm-containment.md). The 09-06 fork storm
is recorded as friction F2026-09-042, and the 09-23 OOM as F2026-09-210.

The v2 additions come from the review-corpus mining in
[tacit-extraction-2026-09-26](../projects/standards/tacit-extraction-2026-09-26.md) (ORB-13088),
and each one cites that candidate's evidence. In evidence, `ORB-nnnnn` ids are ws_orbit tasks, `Fyyyy-mm-nnn`
ids are ws_orbit frictions, and `D:<area>:<n>` is line n of
`docs/design/<area>/4_decisions.md` in Orbit as of 2026-09-26.

**R1.** Suppose a task suspends while it holds a synchronous mutex. Every other task that
needs that mutex blocks an executor thread instead of yielding, so a small runtime deadlocks
and a large one quietly loses throughput. Orbit's `AGENTS.md` bans this, and
`[workspace.lints]` denies it. The same failure happens without any `.await` when a lock or a
transaction spans slow work (candidate K2):
- ORB-11702: companion inference ran inside the SQLite write transaction and store mutex, so
  the store's write lock was held for the length of a model call.
- ORB-11692 and D:policy-sandbox:156: the signal-handler install mutex was held for each child's
  whole lifetime, which "silently serialized every supervised subprocess" in the daemon. Orbit's
  `SignalHandlerGuard` now holds it only for the refcount and `sigaction` section.
- ORB-11614 and ORB-11615: `/api/log` and other runtime calls that lock or scan ran on async
  workers. They now go through the blocking pool. In F2026-07-119, eleven concurrent task
  updates through the API all timed out or were refused while the server was otherwise
  healthy.

A single-flight gate is the exception because serializing the slow work is its whole purpose:
Orbit's `RuntimeMemo` holds a per-key async gate across one compute so that overlapping polls
share it.
*Rust:* use `std::sync::Mutex` for short synchronous sections, `tokio::sync::Mutex` only where
the section must await, and `tokio::task::spawn_blocking` for blocking work. *Python:* never
hold a `threading.Lock` across `await`; use `asyncio.Lock`, and `asyncio.to_thread` for
blocking calls.

**R2.** An unbounded queue turns a slow consumer into memory growth. Memory exhaustion is
exactly what took the host down on 09-23, although a runaway process tree caused it rather
than a queue. Making the full-queue behaviour explicit forces the author to decide what
overload looks like. Orbit's log stream shows all three parts:
- a semaphore caps concurrent streams, and the handler refuses a new stream when no permit is
  free;
- each stream has a depth-64 channel;
- `blocking_send` backpressures the reader thread.

*Python:* `queue.Queue(maxsize=N)` and `asyncio.Queue(maxsize=N)`.

**R3.** Two locks taken in opposite orders on two paths is the classic deadlock. Orbit's task
commit boundary requires every participant to take the partition boundary lock *before* any
per-bundle lock. It makes nested acquisition on the same thread re-entrant rather than
self-deadlocking.

**R4.** This rule comes from the **09-06 fork storm**. A fixture wrote its release sentinel
only on the normal success path. When an assertion panicked, the parent timed out, or the
temp directory was dropped, the waiting child had no owner and no way to exit. Orbit's guards
show the required shape:
- `AuditGuard` starts in the `Failure` state and wraps its `Drop` side effect in
  `catch_unwind`, so a failure during cleanup cannot panic twice.
- `StagedTextFile` rolls back unless `commit()` was called.

*Rust:* mark guard types `#[must_use]`. *Python:* a context manager or a pytest fixture that
cleans up in `finally` after `yield`.

**R5.** A crash or a full disk in the middle of an in-place rewrite leaves a torn file that
the next reader cannot parse. Orbit merged three separate atomic-write helpers into one module.
It makes durable writes (rename plus parent-directory `fsync`) the default and offers a
`volatile` variant only where a caller accepts losing the write in a crash. ORB-12165 shows why
one helper matters: a side path rewrote all of `~/.claude.json` with an unlocked, non-atomic
write. Multi-file records need the same guarantee at directory level (candidate T11). In
F2026-07-097, one half-created task directory, missing its review-threads subdirectory, made
every later task read and create in the workspace fail; the friction's own remedy was "creation
should be atomic (temp dir + rename)". Orbit now stages whole task bundles in a sibling
directory and publishes them by rename. *Python:*
`tempfile.NamedTemporaryFile(dir=parent, delete=False)`, then `os.fsync`, then `os.replace`.

**R6.** A lock file that marks the lock by merely existing stays behind when its holder
crashes, and wedges every later run. A kernel advisory lock is released when the process dies.
The descriptor must be close-on-exec because a child forked while the lock is held inherits
it. ORB-12532 is a real case: a refused lock with no recorded holder turned out to be a
descriptor inherited by a child that had not yet reached `execve`.

The lock has to cover the whole read-modify-write, not just the write (candidate T11). Each of
these was a lost update or a duplicate, caused by an atomic load and an atomic save with nothing
held between them:
- ORB-12240: `--workspace` resolution saved the global registry outside `with_registry_lock`,
  so a concurrent `workspace init` registration could be lost.
- ORB-11708: a check-then-write race in `ensure_host_identity` created two machine ids.
- ORB-11100: checkoutless task updates raced lifecycle changes.
- ORB-11650: store schema migrations ran concurrently across processes until they took an
  immediate transaction and re-checked inside it.

Where no lock can span both steps, such as a remote branch, compare-and-set against the observed
version does the same job (D:task-publication:115; see R32).
*Rust:* since Rust 1.89, `std::fs::File` has `lock`, `try_lock`, `lock_shared` and `unlock`.
These are advisory locks (`flock` on Linux, `LockFileEx` on Windows) released when the file is
closed, and std opens files close-on-exec, so a new project needs no locking crate (Orbit
predates this and uses `fs2`). *Python:* `fcntl.flock(fd, fcntl.LOCK_EX)`; Python descriptors
are non-inheritable by default.

**R7.** An unbounded wait makes every command hang silently behind one stuck holder. Orbit's
default acquisition deadline is 30 seconds. The timeout error names the holder's PID, the time
it acquired the lock, and its label, so an operator can act on it. The record is written after
the lock is taken, so there is always a short window where a waiter sees the previous holder's
record or an empty one. That is acceptable because the record only explains a timeout; the
kernel lock is the only authority. Orbit treats a refusal with no readable holder as contention,
not as a free lock (ORB-12532).

**R8.** Orbit's task transition, its registry projection and its host reservation live in three
stores that cannot commit together, and a published `task.yaml` cannot be un-published.
Chaining three commits leaves a half-published fact after a crash. The boundary instead:
- records one decision, the journal row flipping to `committed` inside one SQLite transaction;
- replays everything else from that decision.

For a single store, one transaction or a pending-write guard is enough.

**R9.** The task commit boundary spells out this rule. Recovery runs before any state is
exposed. A replay that fails leaves the pending marker in place and keeps the partition closed.
"A crash after the decision leaves a repair obligation, never a successful partial
settlement." Recovery also does not infer failure from age or from a reservation expiring. A
reader who sees half-applied state, or an obligation dropped without a trace, is worse off
than one who gets a refusal.

**R10.** Before ORB-12434, every schema bump was a flag day. Older Orbit binaries still in use
included:
- stale installs on `PATH`;
- MCP servers pinned to an older release;
- workers that had inherited `ORBIT_BIN`.

Each of them either failed every command or needed out-of-band workarounds (F2026-08-063/064,
and the 2026-09-13 deploy script). The version number alone cannot tell an older binary whether
newer state is safe to read. So the newer binary records which migrations would break older
readers, and the older binary reads that record. Read-only access must be *enforced*, not
promised: Orbit sets `PRAGMA query_only=ON` on the database connection, and the store refuses
writes with a scoped error. No recorded Orbit incident came from an older binary corrupting
newer state, and this rule is why.

The compatibility claim is the weak point, so it defaults to the safe answer (candidate P2).
Orbit's decision reads: "`Additive` is a claim about binaries that *lack* the migration...
Anything that removes, renames, or reinterprets state older binaries touch is `Breaking`.
Declare `Breaking` when in doubt" (D:state-compatibility:53). A wrong "incompatible" costs an
upgrade; a wrong "compatible" lets an old binary misread live state. The enforcement at the
storage layer holds "for code that has not been written yet" (D:state-compatibility:97,
D:state-compatibility:103).

**R11.** Without a process group, an orphaned grandchild keeps the output pipes open, and the
wait on the child hangs forever (see the comment in `orbit-exec`'s spawn path, and
D:policy-sandbox:127). It also leaves nothing to signal as a unit when the child must be
stopped. *Python:* `subprocess.Popen(..., start_new_session=True)`, or `process_group=0` on
Python 3.11 and later.

**R12.** SIGTERM, then a grace period, then SIGKILL is the pattern that tini, dumb-init and
Kubernetes use. It gives well-behaved children time to flush their output and remove temp
state, and it still guarantees an end. Orbit uses a 5-second grace period (D:policy-sandbox:144,
D:activity-job:591). It polls the group with `killpg(pgid, 0)` and treats any answer other than
`ESRCH` as alive, so an uncertain probe errs toward SIGKILL. The lead child exiting says nothing
about the rest of its group. *Python:* `os.killpg(pgid, signal.SIGTERM)`, then
`proc.wait(timeout=grace)`, then `os.killpg(pgid, signal.SIGKILL)`.

**R13.** A clean exit by the child does not mean its descendants have exited. A child that is
never waited on becomes a zombie and holds a PID slot. Orbit kills the whole group even after a
clean exit, then joins its output readers (D:activity-job:591). The report matters as much as
the reaping. In F2026-07-167, a background command that died with exit 127
(`cargo: command not found`) was reported as "completed (exit code 0)", and two validation steps
were believed green while nothing had compiled. Orbit's supervisor carries `timed_out` as its
own field beside the exit code and annotates stderr with the timeout or signal, so a caller
never has to guess from text.

**R14.** After the child is reaped, the kernel may give its PID to an unrelated process. Sending
`killpg` to that number would signal processes the supervisor never spawned. Orbit registers
only a verified live group leader. Its signal handler checks again before each `killpg`, and
the waiter releases the registration as soon as the child is reaped.

The same hazard applies to every stored reference to a process, not only to signalling
(candidate K5). Orbit's decisions say "The table must never hold a bare pid"
(D:policy-sandbox:161) and "PID reuse cannot fake a live agent: a live PID whose versioned
start-identity token disagrees reads as `exited`" (D:auditability:491). Orbit's token records
the PID namespace too (ORB-10594), because a sandboxed caller in a private namespace cannot see
host PIDs at all. F2026-09-203 shows the other half of the rule. A pid-reuse hardening added a
`/proc/<pid>/stat` read to plugin callback authorization. Landlock refused that read inside the
sandbox, the failure was treated as "no matching record", and for about 50 minutes a confined
plugin could call tools outside its allowlist. A probe the subject can make fail must never
resolve to the permissive answer.

**R15.** A child that fills a pipe the parent is not reading blocks forever, and output capture
with no byte limit is one more unbounded buffer. Orbit's exec contract requires background
drain threads that start immediately after spawn. It terminates the group when the output
limit is reached. Captured text stays dangerous after the child is gone (candidate K3). In
ORB-12467, a 2.5 MB `primary_checkout_drift` diagnostic was passed whole into the recovery
agent's prompt, which then exceeded the provider's input limit, so recovery never started.
Orbit now bounds each embedded field, marks the cut, and points at the untruncated copy in the
run audit.

**R16.** In both host outages, `setsid()`-detached workers escaped every cleanup that relied on
the process group. On 09-23, the agent's `cargo test` passed in 0.78 seconds while the recursion
carried on in the background.

The unit that launches a detached worker sets that worker's fate:
- F2026-07-016: `setsid` changes the session but not the cgroup. A oneshot unit with
  `KillMode=control-group` therefore SIGKILLed workers before they could start. F2026-07-064
  records the same lesson: session detachment alone does not escape a systemd cgroup.
- 09-23: `KillMode=process` on `orbit-web.service` let a runaway tree survive every OOM kill
  and restart of the service.

Giving each worker its own bounded scope with an owner record resolves both. This is how Orbit
fixed it in ORB-12903.

**R17.** This rule also comes from the **09-06 fork storm**. The waiter was
`while [ ! -f "$SENTINEL" ]; do sleep 0.01; done`. `sleep` is a separate process, so each leaked
waiter tried about 100 process launches per second. The host had 382 leaked waiters, which
implies about 38,200 launches per second. Load average reached 393 on 14 CPUs.

An in-process wait with a deadline and a check on the child's exit status cannot multiply
this way. `docs/LESSONS.md` §3 is the source of this rule. *Python:* a loop with a deadline
that calls `time.sleep` and checks `proc.poll()`, not `subprocess.run(["sleep", ...])`.

**R18.** On 09-06, the fixture had no guard that would terminate and reap its child during
unwinding, so every failed test run leaked one more waiter. A guard that kills and waits on
drop makes a failed test leave no process behind.

**R19.** This rule comes from both incidents. When a test calls a production path that
re-executes `current_exe()`, it starts the *libtest harness*, not the product. libtest reads
the worker arguments as test-name filters, selects the spawning test again, and recurses.
F2026-08-075 recorded this trap a month before the first incident.

- **The first fix was not enough.** ORB-11418 put a test-only substitute behind `#[cfg(test)]`.
  That flag is set only when the defining crate is itself under test, so a downstream crate's
  test build compiled the guard out. F2026-09-083 is the same `#[cfg(test)]` scoping biting a
  build instead of a guard.
- **09-23:** 1,320 to 2,227 live harness processes, about 18 GiB of memory, a global OOM, and a
  hard reset.
- **The current fix, ORB-12902:** a runtime check refuses any executable shaped like a cargo
  test harness (`deps/<crate>-<16 hex>`), independent of compile flags.

Compile-time test flags are not a safety boundary.

**R20.** Running `export HOME=/tmp/x` in a shell is not isolation. Inherited authority variables
such as a managed-run context, a registry root or a workspace name take precedence over home
discovery. A fixture that relies on the shell export can then mutate the operator's real store.
Orbit's fixtures clear a shared list of inherited-authority variables on the child command.
Nextest test groups serialize the few test modules that change the process environment or the
current directory. *Python:* `monkeypatch.setenv("HOME", str(tmp_path))` in pytest, plus
passing an explicit `env=` to child processes. STD-04 builds on this rule for test isolation
in general.

**R21.** On 09-06, the run sandbox outlived its finished task for about 113 minutes, and only
cancelling the run collapsed the process tree. On 09-23, the runaway tree survived restarts of
the service. `docs/LESSONS.md` §3 states the lesson directly: "fixture cleanup and runtime
containment are separate safety layers." Either layer can fail by itself. Rules R17 to R20 make
the fixture layer good, and this rule requires the second layer anyway.

**R22.** On 09-23, the dispatching service ran with `MemoryMax=infinity` and `TasksMax=32710`,
so nothing stopped the growth before the kernel's global OOM killer froze the guest. On 09-06,
a three-hour agent wall timeout gave a leak time to compound. Orbit's worker scopes now default
to `MemoryHigh` at 40% of RAM, `MemoryMax` at 50% and `TasksMax=4096`, with `OOMPolicy=continue`,
so a breach kills a process inside the run and not the host.

The outer ceiling does not excuse unbounded waits inside the process (candidate K3). ORB-11696
added a deadline to companion RPC reads. ORB-11761 then bounded stalled
companion downloads "without restoring a whole-request deadline": a fixed ceiling on the whole
transfer fails a large download on a slow link, while an idle deadline ends only a stall. That
companion was removed on 2026-09-20; Orbit's plugin MCP backend states the surviving form: "There
is no code path that waits without a deadline." F2026-07-122 is the handle case: a synchronous
`workflow_run_resume` wedged the API while it ran, and the remedy was to return a handle.

**R23.** Candidate P2. Orbit treats its v1 baseline as "an immutable historical artifact guarded
by a structural fingerprint. Every schema change after v1 must use a new append-only migration,
and tests compare a fresh database with a v1 database upgraded through every registered version"
(D:activity-job:747). F2026-07-106 is why the parity test exists. Columns added to the v1
baseline's idempotent helpers were written as if they still ran on every open, but under the
ledger v1 runs once, so existing databases never received them while fresh databases did. Only
an appended migration reaches both. Editing a shipped migration forks the schema between
installs that ran the old text and installs that run the new one, and the ledger cannot tell
them apart. One transaction per migration, together with its ledger row, means an interrupted
migration rolls back instead of leaving half-applied schema.

**R24.** Candidate C6. In ORB-12619, a crew whose name contained `:` could be defined and used,
but putting it in a complexity pool refused every command in the workspace. The fix added a
crew-name check, and ORB-12625 found the consequence: "A config that already names a crew with
`:` is bricked by the new crew-name check, with no CLI left that can remove it", since every
`orbit config` subcommand loads the same config first. Orbit's fix is the minimum this rule
allows: the refusal names the exact file and the table to rename by hand. Where a migration is possible, prefer it. Orbit's process-identity
tokens show the grandfathered form: v1 tokens are "still read (a run claimed by an older binary
outlives its upgrade), never written".

**R25.** Candidate P6, a SHOULD because not every project ships defaults into user space. In
ORB-11745, `orbit init` without `--force` reset an operator's `executor sandbox: off`, which
silently undid a deliberate choice. ORB-10684 required retiring managed assets "without deleting
user-authored resources". The resulting decision (D:activity-job:1346) persists "the SHA-256
digest last written for each bundled activity or job"; refresh "removes retired files only when
their bytes still match that digest" and moves locally modified retired files into a backup
area. A digest separates "the tool's file, unchanged" from "the user's file", and neither a path
nor a timestamp can.

**R26.** Candidate P7, a SHOULD because the mechanism depends on how a project is installed.
F2026-07-011 found a change that reached only fresh initialization: "existing initialized
workspaces were unaffected". F2026-07-183 found a deploy that did not reach running workers:
"replacing the binary on disk does not affect already-running processes", so a feature looked
broken when it had never loaded. ORB-13013 found the opposite hazard: installing a new binary
SIGTERMed live drains and recorded them inconsistently. `orbit update` now lets the replacement
executable run migrations and managed-asset reconciliation, because only the new binary carries
the new definitions. An executable-generation record admits one executable generation per state
authority, so old and new binaries do not silently mix.

**R27.** Candidate K1. Orbit's worktree setup once published only the *name* of its start point
(`origin/<base>`), and the commit step re-resolved it. That ref is shared by every worktree off
one `.git`, so any sibling's fetch or any merge moved it, and "each new run's fetch retroactively
invalidated every older in-flight run" (D:activity-job:1020). F2026-07-113 counted 5 of 8 runs
failing on one day. The fix resolves the start point once, emits `base_sha`, and rejects a ref
name where the pinned id is expected. ORB-12655 repeated the defect in claimed-leaf validation.
The re-read half comes from F2026-07-184. The integrity guard snapshotted the task's admitted
scope before the agent was allowed to widen it, so correct in-scope changes were reported as off
scope; "the guard must re-read the task record at verify time".

**R28.** Candidate T17. Orbit's post-run integrity guard originally failed on any change to the
shared primary checkout that was not a clean fast-forward. "Every merge detonates every pipeline
run in flight at that moment" (F2026-07-101, recurring as F2026-07-139). In F2026-07-166, "a
complete, validated, test-green implementation was discarded at the gate by an unrelated
concurrent write": a sibling run had rewritten 12 learning files. In F2026-07-160, removing other
runs' files to satisfy the guard "would have destroyed their only copy". The guard now treats a
stationary primary HEAD and a proven same-branch fast-forward as benign. It leaves the real
candidate-versus-target question to the later rebase against the fetched base, where Git can
tell a clean merge from a conflict. Unexplained changes still fail closed.

**R29.** Candidate T4, with two data-loss incidents:
- F2026-09-152: `workspace teardown --confirm`, run from an unregistered checkout, resolved an
  ancestor `.orbit` and `remove_dir_all`'d another workspace's task-store partition. Twelve task
  bundles were lost, with no backup.
- ORB-12131: `--fix-orphan-task-stores` "still deletes live task bundles".

The same shape recurs in ORB-12143, which deleted a partition "whenever the bound checkout is
momentarily unreachable". ORB-12119 classified live partitions as orphaned, and ORB-12680 and
ORB-12688 had reindex delete "data-bearing partial copies". In ORB-12032, a `git reset --hard`
path was "silently destroying uncommitted work". ORB-12800's recommended command made the host
process recursively delete an unrelated directory. The decisions state the positive form:
- "On timeout, mutate only state this attempt owns" (D:activity-job:1441).
- Retire only files whose bytes still match the recorded digest (D:activity-job:1346).
- "A bundle is never easier to discard than its least-settled member" (D:activity-job:1064).

Orbit's stub reaper deletes a bundle directory only when it is provably empty residue:
"Unreadable directories are not stubs (fail closed: do not reap)." Kept data costs disk; wrongly
deleted data is gone.

**R30.** Candidate P1. In F2026-07-096, an integrity gate correctly refused a dirty worktree, but
failure cleanup then removed the worktree, and about 201 files of finished implementation were
lost: "no commit, no stash, no dangling git object". The implementation could be recovered only
from transcripts. Orbit's failure handoff now commits dirty work locally *before* pushing or
opening a PR, "so terminal runs do not strand uncommitted changes" when those fallible steps fail
(D:activity-job:1011). Integrity failures stay "byte-for-byte recoverable after forced
linked-worktree cleanup" (D:activity-job:1192). R4's rollback guards undo side effects; this
rule keeps the *product* of the work, which a rollback must not discard.

**R31.** Candidate K4. Once a request is written, the destination may have committed it, so
calling a lost reply "failed" invites a duplicate retry, and calling it "unreachable" is simply
false. Orbit's federated MCP gives a routed `tools/call` its own delivery budget from the moment
the request is written. "A lost answer after that write — budget exceeded or session ended — is
`outcome_unknown`", carrying the request id (D:federated-mcp:118). The distributed drain gives
every pull a durable request id, so "retries can recover the original admission without
consuming another task" (D:distributed-drain:258). It also refuses automatic reclamation: "TTL
expiry is not proof of death" (D:distributed-drain:47), and "age and TTL support inspection only"
(D:distributed-drain:254). F2026-08-030 is the failure this prevents: live runs were marked
`interrupted`. R9 applies the same principle inside one host.

**R32.** Candidate K6. ORB-12631: owner landing "judges containment against a remote-tracking ref
it never fetches", so a stale `origin/<branch>` stopped valid landings. A remote-tracking ref is
only as fresh as the last fetch. ORB-11110: a publication push accepted branch deletion after
observing a tip, because an ordinary push checks only the new commit, not the tip it replaces.
ORB-11525: a managed PR merge did not atomically pin the reviewed head, so a later push could
merge unreviewed. Orbit's publication decision (D:task-publication:115) reads: "Every future
publisher must compare-and-swap against the fetched branch tip. A non-fast-forward result stops
publication and surfaces an authority conflict". In Git, the compare-and-set is
`git push --force-with-lease=<ref>:<expected-sha>` (plus `--atomic` for several refs), and the
pinned merge is `gh pr merge --match-head-commit <sha>` or the merge API's `sha` field.

## Exemplars

Paths are relative to `codebases/orbit/`.

- R1 — `Cargo.toml#[workspace.lints.clippy]` (`await_holding_lock = "deny"`);
  `crates/orbit-web/src/runtime_memo.rs#RuntimeMemo` (std `Mutex` for the map, a
  `tokio::sync::Mutex` per-key single-flight gate for the compute that awaits);
  `crates/orbit-common/src/test_env.rs#ScopedEnv` (its doc comment says to drop the guard before
  an await); `crates/orbit-exec/src/supervision/signal.rs#SignalHandlerGuard` (install mutex
  held only for the refcount section, never across a child's lifetime);
  `crates/orbit-web/src/api/tasks.rs` (`tokio::task::spawn_blocking` around runtime calls).
- R2 — `crates/orbit-web/src/api/log.rs#spawn_log_sse_frames` (`LOG_STREAM_CHANNEL_DEPTH`,
  `blocking_send`) and `#stream_log` (refuses when no `LogStreamGate` permit is free);
  `crates/orbit-tools/src/plugin/mcp.rs` (`sync_channel(LINE_QUEUE)`).
- R3 — `docs/design-patterns/task_commit_boundary.md#The two mechanisms`;
  `crates/orbit-store/src/repository/task/coordination/boundary.rs#TaskCommitBoundary::enter_ordinary`
  and `#with_admission`.
- R4 — `docs/design-patterns/raii_guard.md`; `crates/orbit-cli/src/audit_middleware.rs#AuditGuard`;
  `crates/orbit-common/src/fs/io.rs#StagedTextFile`.
- R5 — `crates/orbit-common/src/fs/io.rs#atomic_write_bytes` (durable) and
  `#atomic_write_text_volatile`; `crates/orbit-store/src/fs/yaml.rs#write_yaml_durable_with`;
  `crates/orbit-store/src/driver/file/task_bundle/bundle_io/write.rs#write_bundle_atomically`
  (a multi-file bundle staged in a sibling directory, synced, then renamed).
- R6 — `crates/orbit-common/src/fs/file_lock.rs#try_acquire_exclusive_file_lock` (the `O_CLOEXEC`
  and inherited-descriptor notes, ORB-12532); `crates/orbit-store/src/fs/lock/mod.rs#FileLockGuard`;
  `crates/orbit-registry/src/workspace_registry/io.rs#with_registry_lock` (wraps the whole
  load-edit-save); `crates/orbit-store/src/driver/sqlite/migration/ledger.rs#apply_one`
  (`BEGIN IMMEDIATE`, then re-reads the version inside the transaction).
- R7 — `crates/orbit-common/src/fs/file_lock.rs#DEFAULT_FILE_LOCK_TIMEOUT` and `#FileLockTimeout`
  (carries `holder`), and `#FileLockHolderInfo` ("advisory metadata").
- R8 — `crates/orbit-store/src/repository/task/coordination/commit.rs#commit_task_transition`;
  `crates/orbit-store/src/driver/sqlite/task_commit_journal/mod.rs`; for a single store,
  `crates/orbit-store/src/driver/file/task_bundle/bundle_io/commit.rs#PendingWriteGuard`.
- R9 — `crates/orbit-store/src/repository/task/coordination/commit.rs#recover` and
  `#recover_if_pending`; `docs/design-patterns/task_commit_boundary.md#The two mechanisms`
  ("Recovery before exposure").
- R10 — `crates/orbit-store/src/contracts/compat.rs#evaluate_newer_state`;
  `crates/orbit-store/src/driver/sqlite/connection.rs` (`query_only` pinning);
  `docs/design/state-compatibility/2_design.md#3 The decision` and `#4 Read-only is enforced, not promised`;
  `docs/design/state-compatibility/4_decisions.md` ("Declare `Breaking` when in doubt").
- R11 — `crates/orbit-exec/src/process.rs#command` (`command.process_group(0)`).
- R12 — `crates/orbit-exec/src/supervision/cleanup.rs#terminate_process_group`
  (`TERMINATION_GRACE_PERIOD`) and `#process_group_is_alive` (anything but `ESRCH` is alive);
  `docs/design/policy-sandbox/1_overview.md#2.5 Exec supervision is not default OS isolation`;
  `docs/design/policy-sandbox/2_design.md#8. Process Supervision`.
- R13 — `crates/orbit-exec/src/supervision/wait.rs#wait_with_timeout_and_output_limit`
  (clean-exit `kill_process_group` before the reader joins) and `#WaitResult` (`timed_out`
  beside `exit_code`).
- R14 — `crates/orbit-exec/src/supervision/signal.rs#SignalHandlerGuard` (`release_process_group`);
  `crates/orbit-exec/src/supervision/tests/signal.rs`;
  `crates/orbit-common/src/process/identity.rs#probe_process_liveness` and `#ProbeOutcome`
  (PID plus a versioned start-identity token with its PID namespace; an unavailable probe is not
  proof of death).
- R15 — `crates/orbit-exec/src/supervision/tee.rs#spawn_stdout_drain`;
  `docs/design/policy-sandbox/specs/sandbox-exec-contract.md#Supervision Invariants`;
  `crates/orbit-engine/src/activity_job/job_executor/recovery.rs#bounded_recovery_text`
  (`MAX_RECOVERY_ERROR_MESSAGE_BYTES`, marked truncation, full text in the run audit).
- R16 — `crates/orbit-core/src/application/job/pipeline/worker/scope.rs#contain_worker_command`
  and `#WorkerLimits`; `crates/orbit-core/src/application/job/pipeline/worker/supervisor.rs#spawn_process`
  (`setsid` plus `register_worker_process`).
- R17 — `crates/orbit-cli/tests/web_serve_root.rs#wait_for_listening` (deadline plus
  `try_wait`); `crates/orbit-common/src/test_process.rs#retry_executable_busy`.
- R18 — `crates/orbit-core/src/application/job/run/tests/owner.rs#ReapingChild`;
  `crates/orbit-cli/tests/mcp_roundtrip.rs#McpClient` (`Drop` kills and waits).
- R19 — `crates/orbit-core/src/application/job/pipeline/worker/command.rs#refuse_test_harness_worker`
  and `#worker_command_override`; `crates/orbit-core/src/lib.rs` (`test_support::install_substitute_pipeline_worker`);
  `docs/DEVELOPMENT.md#Tests that submit pipeline runs`.
- R20 — `docs/DEVELOPMENT.md#Safe Mutable CLI Fixtures`;
  `crates/orbit-common/src/test_env.rs#clear_inherited_authority`;
  `crates/orbit-cli/tests/ambient_authority_isolation.rs`; `.config/nextest.toml` (`stateful-runtime`
  test group).
- R21 — `docs/LESSONS.md#3. The September 2026 Test Fixture Fork Storm`;
  `crates/orbit-core/src/application/job/pipeline/worker/scope.rs` (module doc).
- R22 — `crates/orbit-config/src/registry/settings.rs#DEFAULT_WORKER_TASKS_MAX` (with
  `DEFAULT_WORKER_MEMORY_HIGH` and `DEFAULT_WORKER_MEMORY_MAX`);
  `crates/orbit-exec/src/supervision/wait.rs#wait_with_optional_timeout`;
  `crates/orbit-tools/src/plugin/mcp.rs` (module doc: "no code path that waits without a
  deadline"; a reader thread makes the per-request `recv_timeout` real);
  `crates/orbit-mcp/src/federated/probe.rs#DEFAULT_ROUTED_DELIVERY_TIMEOUT` (budget measured
  from when the request is written, not from session start).
- R23 — `crates/orbit-store/src/driver/sqlite/migration/ledger.rs#MIGRATIONS`,
  `#validate_registry` (strictly increasing) and `#apply_one` (one transaction with its ledger
  row); `crates/orbit-store/src/driver/sqlite/migration/tests/ledger.rs`
  (`shipped_v1_baseline_structure_is_frozen`, `v1_upgrade_and_fresh_database_have_identical_columns`).
- R24 — `crates/orbit-common/src/process/identity.rs#STABLE_TOKEN_PREFIX_V1` (old format read,
  never written); `crates/orbit-config/src/crew_pools.rs#reject_unpoolable_crew_name_in_config`
  (the fallback: the refusal names the file and the table to edit by hand).
- R25 — `crates/orbit-core/src/application/managed_assets.rs#reconcile_managed_assets_in_mode`
  and `#preserve_modified_retired_asset` (`.orbit-managed-assets.json` digests);
  `crates/orbit-core/src/application/routines/tests/materialize.rs`.
- R26 — `crates/orbit-cmd/src/update/mod.rs` (module doc: the replacement executable migrates
  state and reconciles managed assets); `crates/orbit-cmd/src/update/converge.rs`;
  `crates/orbit-common/src/fs/generation.rs#GenerationGuard`; `docs/runbooks/upgrades.md`.
- R27 — `crates/orbit-engine/src/executor/automation/vcs/worktree/setup.rs#setup_worktree`
  (resolves `base_sha` once, ORB-10380);
  `crates/orbit-engine/src/executor/automation/vcs/commit/mod.rs#validate_pinned_head` and
  `#pinned_object_id` (rejects a ref name where the pinned id belongs).
- R28 — `crates/orbit-engine/src/activity_job/workspace/boundary_guard.rs#verify_after_provider`,
  `#primary_stationary_dirt_delta_is_benign` and `#primary_fast_forward_is_benign`;
  `crates/orbit-engine/src/activity_job/cli_runner/tests/orchestrator_worktree.rs`
  (`two_in_flight_worktrees_survive_one_primary_merge_advance`).
- R29 — `crates/orbit-store/src/driver/file/task_bundle/bundle_io/stub.rs#is_unpublished_stub`
  and `#reap_unpublished_stub` (unreadable or data-bearing directories are kept);
  `crates/orbit-cmd/src/doctor/task.rs#doctor_check_orphan_task_stores`;
  `docs/design/activity-job/4_decisions.md` ("On timeout, mutate only state this attempt owns").
- R30 — `crates/orbit-engine/src/executor/automation/vcs/failure.rs#pr_failure_handoff`
  (`commit_failure_candidate` before push and PR);
  `crates/orbit-engine/src/executor/automation/vcs/pr/tests/handoff.rs`
  (`non_fast_forward_drift_handoff_commits_dirty_work_and_raises_pr`).
- R31 — `crates/orbit-mcp/src/federated/probe.rs#call_tool` (`LostAnswer::OutcomeUnknown`) and
  `#DEFAULT_ROUTED_DELIVERY_TIMEOUT`; `crates/orbit-mcp/src/federated/host.rs#delivery_unreachable`
  (never relabels a dispatched call); `docs/design/distributed-drain/4_decisions.md`.
- R32 — `crates/orbit-store/src/workflow/task/publish.rs#observe_remote` (fetches before
  judging the tip), `#push_fast_forward` and `#compare_and_swap_lease`;
  `crates/orbit-engine/src/executor/automation/vcs/operations.rs#push`
  (`--force-with-lease=refs/heads/<branch>:<expected_remote_sha>`).

## Machine checks

Each gate below is real and can be adopted. "Orbit:" says whether Orbit enforces it today.

| Rule | Gate |
|---|---|
| R1 | Rust: set `clippy::await_holding_lock = "deny"` and `clippy::await_holding_refcell_ref` in `[workspace.lints.clippy]`, and run clippy with `-D warnings`. For other guard types, use `clippy::await_holding_invalid_type` with `await-holding-invalid-types` in `clippy.toml`. None of these lints sees `tokio::sync` guards, which are allowed by design. Orbit: `await_holding_lock` is denied; the other two are not set. Locks and transactions across slow work, and blocking calls on async threads: review-only in every language. |
| R2 | Rust: `clippy::disallowed_methods`, listing `std::sync::mpsc::channel`, `tokio::sync::mpsc::unbounded_channel` and `crossbeam_channel::unbounded` under `disallowed-methods` in `clippy.toml`. The full-queue behaviour is review-only. Orbit: not configured, so review-only. |
| R3 | review-only |
| R4 | Rust: the rustc lint `let_underscore_lock` (deny by default) catches a guard dropped immediately. Put `#[must_use]` on guard types so `unused_must_use` fires. Everything else: review-only. |
| R5 | Rust: `clippy::disallowed_methods` listing `std::fs::write` (and any other direct-write entry point the project wants funnelled) in `clippy.toml`, with `#[allow(clippy::disallowed_methods)]` only inside the helper module. Orbit: not configured, so review-only. |
| R6 | review-only; add a test that runs two read-modify-write updaters concurrently and asserts neither update is lost |
| R7 | review-only; add a unit test that covers the timeout path and asserts the holder appears in the error |
| R8 | review-only; add fault-injection tests that crash before and after the commit point (Orbit: `crates/orbit-store/src/repository/task/coordination/faults.rs`) |
| R9 | Same fault-injection tests as R8, asserting that a failed replay leaves the store refused, not open |
| R10 | Test: stamp a newer version and a compatibility record into a fixture store, then assert a read-only open with writes refused, or a refusal (Orbit: `crates/orbit-store/src/driver/sqlite/migration/tests/ledger.rs`, `crates/orbit-store/src/workflow/layout/tests/mod.rs`). The compatibility classification itself is review-only. |
| R11 | review-only; add a test that a grandchild dies with its group |
| R12 | review-only; add a test that a SIGTERM-ignoring child is SIGKILLed after the grace period |
| R13 | Rust: `clippy::zombie_processes` flags a spawned child that is never waited on (Orbit: not set explicitly). The group sweep and the termination report are review-only; add a test that a child killed on timeout is not reported as success. |
| R14 | Test: signal only live, owned group leaders (Orbit: `crates/orbit-exec/src/supervision/tests/signal.rs`); a live PID with a foreign start-identity token reads as exited (Orbit: `crates/orbit-common/src/process/tests/identity.rs`, `live_pid_with_a_foreign_identity_token_reads_as_exited`) |
| R15 | review-only; add a test with a child that writes more than the pipe buffer, and one that feeds an oversized error through every path that embeds it (Orbit: `crates/orbit-engine/src/activity_job/job_executor/tests/recovery.rs`) |
| R16 | Runtime: launch into a scope with limits, for example `systemd-run --user --scope -p TasksMax=… -p MemoryMax=…`. Orbit: on by default through `machine.worker_containment`. It falls back to the caller's cgroup with a warning when no user manager is reachable. |
| R17 | review-only. An optional CI grep that rejects `sleep 0.0` inside shell `while` loops in test sources would catch the known pattern. Orbit: no such guard. |
| R18 | Rust: `clippy::zombie_processes` is a partial check. Otherwise review-only. |
| R19 | Runtime: fail closed in the spawn path when the executable is a test harness, plus a test that asserts the refusal (Orbit: `refuse_test_harness_worker`) |
| R20 | nextest `[test-groups]` with `max-threads = 1` for tests that change global state (Orbit: `stateful-runtime`). Isolating `HOME` and authority variables is review-only. |
| R21 | Test runner: nextest `slow-timeout = { period = "60s", terminate-after = 2 }` terminates a hung test, and `leak-timeout = { period = "500ms", result = "fail" }` fails a test whose children keep its output open. Runtime: tear down the run's own cgroup or unit when the run ends. Orbit: a per-worker scope; nextest timeouts are not configured. |
| R22 | systemd `TasksMax=`, `MemoryMax=` and `RuntimeMaxSec=` on units and scopes; GitHub Actions `timeout-minutes`; nextest `slow-timeout` and `global-timeout`; pytest `--timeout` from pytest-timeout. Orbit: worker-scope limits are on; CI jobs have no `timeout-minutes`, and nextest has no `slow-timeout`. In-process deadlines: review-only; add a test with a peer that stalls after accepting a request (Orbit: `crates/orbit-mcp/src/federated/tests/probe.rs`). |
| R23 | Test: a frozen structural fingerprint of the released baseline, plus a test that a fresh store and one upgraded from the oldest supported version have identical schemas (Orbit: `shipped_v1_baseline_structure_is_frozen`, `v1_upgrade_and_fresh_database_have_identical_columns`). Runtime: refuse a migration registry whose versions are not strictly increasing (Orbit: `validate_registry`). |
| R24 | review-only; add a fixture of state written by the previous release and assert that the new binary loads it, or refuses naming the file (Orbit: `crates/orbit-config/src/tests/layering.rs`, `a_persisted_colon_named_crew_is_refused_by_file_and_repaired_by_renaming_it`) |
| R25 | Test: an unmodified retired default is retired, and a user-modified one is preserved (Orbit: `crates/orbit-core/src/application/routines/tests/materialize.rs`). Otherwise review-only. |
| R26 | review-only; the deploy procedure runs the new binary's migrate and reconcile steps and restarts long-lived units (Orbit: `orbit update` convergence steps) |
| R27 | Runtime: steps that need the pinned value accept only an immutable id and reject a ref name (Orbit: `pinned_object_id`), plus a test that a sibling moving the named ref does not fail an in-flight run |
| R28 | Test: an unrelated concurrent change to shared state does not trip the guard, and an overlapping one does (Orbit: `two_in_flight_worktrees_survive_one_primary_merge_advance`, `concurrent_primary_fast_forward_does_not_block_disjoint_worktree_changes`) |
| R29 | review-only; add tests that an unreadable, partial or data-bearing target survives the cleanup path (Orbit: `crates/orbit-store/src/repository/task/tests/v2_bundle.rs`, `listing_reports_data_bearing_bundle_missing_envelope`) |
| R30 | review-only; add a fault-injection test that fails a step after work exists and asserts the work is recoverable after teardown (Orbit: `non_fast_forward_drift_handoff_commits_dirty_work_and_raises_pr`) |
| R31 | Test: a peer that stalls or disconnects after the request is written yields outcome unknown, not unreachable (Orbit: `crates/orbit-mcp/src/federated/tests/route.rs`, `a_dispatched_call_whose_answer_is_lost_is_outcome_unknown_not_unreachable`; `crates/orbit-mcp/src/federated/tests/probe.rs`, `a_stall_after_the_tool_call_is_dispatched_is_outcome_unknown`). Automatic reclaim: review-only. |
| R32 | Git: `push --force-with-lease=<ref>:<expected-sha>` (never a bare `--force`) and `--atomic` for several refs; `gh pr merge --match-head-commit <sha>` for a pinned merge. Test: a remote tip that is deleted, rewound or advanced after observation is a conflict (Orbit: `crates/orbit-store/src/workflow/task/tests/publish.rs`). Fetch before judging containment: review-only. |

## Applies to

- `universal`: R1 to R4, R11 to R22, R27 and R28. Any project with concurrency, child
  processes or tests.
- `universal`, where the project does the thing: R29 and R30 for any code that deletes,
  resets or tears down; R31 for any code that sends requests to another process or host; R32
  for any code that automates Git.
- `stateful`: R5 to R10, and R23 to R26 (R25 and R26 are SHOULD). Any project that persists
  state across runs or processes.
- `service`: R16, R22 and R26 bind hardest on long-running daemons, workers and timer jobs.
- `rust`: the Rust gates in §Machine checks.

## Deviations

An adopting repo that departs from a rule records a decision in its own design docs, citing
`STD-03@2 §Rn` and the reason. It never edits its vendored copy of this standard.

Some deviations are expected, and the decision still needs to be recorded:
- A platform without process groups (Windows) meets R11 to R14 with job objects, citing
  `STD-03@2 §R11`.
- A host without a cgroup manager meets R16 and R22 with `setrlimit` (`RLIMIT_NPROC`) plus a
  loud warning, citing `STD-03@2 §R16`. Orbit's own fallback runs the worker in the caller's
  cgroup and warns once per process.

## Changelog

- **v2 (2026-09-26, ORB-13127).** Folded in the accepted candidates from the ORB-13088 tacit
  extraction (disposition recorded on ORB-13088). Every rule number from v1 keeps its meaning.
  - Added R23 (append-only migrations, P2), R24 (grandfather or migrate on stricter validation,
    C6), R25 (SHOULD: reconcile shipped defaults by provenance, P6) and R26 (SHOULD: fixes
    reach existing installs and running processes, P7) to the renamed "Version skew and state
    evolution" cluster.
  - Added R27 (pin moving references once, K1) and R28 (guards trigger on actual interference,
    T17) in the new "Moving inputs and integrity guards" cluster.
  - Added R29 (positive proof of ownership before destruction, T4) and R30 (failure paths
    preserve completed work, P1) in the new "Destructive operations and recovery" cluster.
  - Added R31 (lost reply after dispatch is outcome unknown; no TTL reclaim, K4) and R32 (fetch
    before judging, compare-and-set pushes, pinned merges, K6) in the new "Distributed and
    remote effects" cluster.
  - Extended R1 (no lock or transaction across slow work; blocking calls off async threads,
    K2), R5 (one shared write helper; multi-file records staged and renamed, T11), R6
    (read-modify-write under the owning lock or compare-and-set, T11), R10 (declare a change
    incompatible when in doubt, P2), R14 (process identity is PID plus start identity; an
    unanswerable probe is unknown, K5), R15 (bound captured text passed onward, K3) and R22
    (in-process deadlines on every wait; idle deadlines for streams, K3).
  - Filled T10 gaps: R12 now decides survival by probing the group, with an unanswerable probe
    counted as survival; R13 now requires reporting the child's real termination. Added T10's
    uncited evidence to the Why entries for R16 and R19.
  - Clarified R7: the holder record is advisory and can be briefly stale. Added a note to the
    R6 Why that Rust std `File::lock` and `File::try_lock` (stable since 1.89) meet R6 without
    a crate (from the ORB-13117 template review).
  - "Applies to" is now a bullet list of tags with rule scope, deviation examples cite
    `STD-03@2 §Rn`, and `related` adds STD-01, STD-04 and STD-05.
- **v1 (2026-09-26).** First publication: R1 to R22.
