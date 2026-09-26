---
id: STD-01-cli-surface
title: CLI surface — command grammar, inputs and effects, output modes, errors, help, and golden-tested public surfaces
summary: Normative rules for any constellation CLI's user-facing surface — noun-verb grammar, honoring every input or rejecting it, read-only commands that never write, human vs `--json` output, stdout/stderr split, tables, color/TTY, errors and exit codes, help, goldens, deprecation, no internal IDs; adopt at a pinned version in every new CLI.
status: active
tags: [standard, cli, output, ux, help, errors, deprecation]
version: 2
created: 2026-09-26
updated: 2026-09-26
last_validated: 2026-09-26
related: [STD-02-rust-architecture-and-errors, STD-03-concurrency-and-process-safety, STD-04-testing-and-verification, STD-05-security-boundaries]
---

# CLI surface — command grammar, inputs and effects, output modes, errors, help, and golden-tested public surfaces

A CLI has three readers: a human scanning a terminal, a script parsing a pipe, and an
agent reading through a shell tool. The human reader notices a broken column and a script
fails loudly on bad JSON, but an agent carries on with a value it misread. These rules
detect which reader is present and give it the right bytes. They make the machine-facing
surface a contract that tests pin down, and they make every input either take effect or
fail loudly.

Every rule is portable. A two-command CLI built on `clap` derive (or any argument parser)
can meet all of them without Orbit's operation registry, sink, or payload types. Orbit
appears only as the worked example. Where Orbit uses a heavy mechanism, the rule says what
the minimum compliant version looks like.

Rule numbers are stable. Rules added in v2 take the next free numbers (R26 onward) and sit
in the cluster they belong to, so numbers inside a cluster are not always in order.

## Rules

### Command grammar

- **R1.** Commands MUST follow `<tool> <noun> <verb> [args] [flags]`, where a noun (the
  resource) groups its verbs as subcommands. A CLI with a single resource MAY drop the noun
  level (`<tool> <verb>`), but one tool MUST NOT mix the two orders.
- **R2.** A verb MUST mean the same thing on every noun, and verbs MUST come from one small
  shared vocabulary (`add`, `list`, `show`, `update`, `remove`/`archive`, …). `list`
  returns many records filtered by flags; `show` returns one record by its identifier.
- **R3.** Flags MUST be long-form kebab-case, and one concept MUST have one spelling
  across the whole command tree (`--workspace`, `--limit`, `--status`). A repeatable flag
  uses the singular form and repeats (`--tag a --tag b`). Short aliases are reserved for
  `-h`/`-V` and a few high-frequency flags. The one sanctioned second spelling is `--json`
  for `--format json` (R7). A flag's name is public surface: renaming or removing it is a
  breaking change and follows R35.
- **R4.** Cross-cutting options (context selection, data-root override, output mode) MUST
  be declared once as global flags that are accepted both before and after the subcommand.
  A command MUST NOT redeclare a global flag's name with a different meaning.
- **R26.** A subcommand's argument MUST NOT share its parser-internal identifier with a
  global argument. *clap note:* clap derive takes an argument's id from the field name and
  merges arguments that share an id, so a subcommand positional named `workspace` silently
  becomes the global `--workspace`. It parses, and its value goes to the global. Give such
  a field an explicit distinct id (`#[arg(id = "workspace_selector", value_name =
  "WORKSPACE")]`). A CLI with no global arguments, or a parser that keeps the two
  namespaces apart, complies trivially.
- **R5.** A destructive or irreversible command MUST NOT prompt or read stdin. It MUST
  refuse before changing anything unless `--confirm` is passed. A bulk or cleanup command
  defaults to a report (dry run) and applies only with `--confirm`. Add no other
  confirmation spelling.
- **R27.** An interactive prompt (allowed only for non-destructive commands, R5) SHOULD
  treat EOF or closed stdin as an error, never as an empty answer. It fails once, without
  re-prompting, with a message naming the flags that answer the prompt non-interactively
  (R21). An unanswered prompt on a non-terminal stdin SHOULD be bounded by a timeout
  rather than waiting forever.

### Inputs and effects

- **R28.** Every flag, selector, or override a command accepts, whether as a flag or as
  an environment variable, MUST take effect on every code path the command runs, or the
  command MUST reject it before doing any work. An invocation scoped by a root or context
  override (`--root`, `--workspace`, `TOOL_ROOT`) MUST NOT read or write state outside
  that scope, such as the user's home-level config, global skill links, or installed
  binaries. A command that cannot honor an override refuses it. It does not run unscoped.
- **R29.** A command or write surface MUST apply every field and argument it accepts, or
  reject the whole request and name each offending field. It MUST NOT silently drop,
  clamp, demote, or reinterpret caller input while reporting success. An unknown field is
  an error, not something to ignore. A partial update changes only the fields the caller
  supplied and leaves every other field as it was.
- **R30.** A command MUST NOT report success unless the effect it was asked for actually
  happened. A command that writes something MUST name the resolved target it wrote (the
  file, checkout, workspace, or record) in its human output and include it in its machine
  payload. If the request resolved to nothing (an excluded item, an inactive target), the
  command fails or says so. It does not print a bare "done".
- **R31.** A command that only reports (`list`, `show`, `status`, a check without `--fix`,
  a scan) MUST NOT create files or directories, take write locks, run migrations,
  reconcile or finalize records, or otherwise change durable state. It MUST work against a
  read-only data directory. Setup a read needs, such as first-run bootstrap or a schema
  upgrade, belongs to an explicit mutating command. The read fails with an actionable
  error (R21) if that setup has not been done.
- **R32.** Every identifier or selector the tool prints, in any output mode or on any
  surface (CLI, JSON, MCP, API), MUST be accepted back as input by every command and
  surface that takes that kind of identifier. A key a `show` lists MUST be settable or
  gettable by the matching `set`/`get`, or be labelled as derived and read-only.
- **R33.** A `list` MUST NOT apply a filter the caller did not ask for. Default filters
  (for example, hiding closed records) are either absent or stated in the command's help
  and echoed in the stderr notice. Filters MUST be applied before the limit, so `--limit N`
  returns the first N matching records, not the matches among the first N records.

### Output modes and streams

- **R6.** A command that returns data MUST build one structured payload and derive every
  rendering from it, human and machine. The human view may drop or reformat fields. It
  MUST NOT show a value the payload lacks or leave out a record the payload includes.
- **R7.** Every data-returning command MUST offer a machine mode, at minimum `--json`. That
  mode emits one JSON document: an array for a list (or the R34 envelope for a capped
  list), an object for a single record. A streaming command SHOULD also offer NDJSON: one
  complete JSON value per line, flushed as each record is written. Where the tool also has
  a `--format <mode>` flag, `--json` is the one permitted shorthand for `--format json`.
  Passing both with different modes MUST be a usage error (R20), not a silent precedence
  pick. Check the combination once after parsing, in the mode resolver (R8), because
  clap's `conflicts_with` does not reliably see a global argument that is given at a
  different subcommand level.
- **R8.** The output mode MUST be resolved once per invocation, in one place, with this
  precedence: explicit flag > environment variable > `auto`. `auto` renders for a human on
  a terminal and renders the piped form (R9) otherwise. An unrecognized environment value
  falls back to `auto` rather than failing the command.
- **R9.** When stdout is not a terminal, output MUST carry no ANSI escapes, no box-drawing
  or other decoration, and no width-based truncation. The default piped form SHOULD be one
  tab-separated line per record with no header, so `cut -f`, `grep`, and `wc -l` work
  without flags.
- **R10.** Machine output is a public contract. Field names MUST be stable `snake_case`.
  Renaming, removing, or retyping a field is a breaking change. So is changing the
  document's top-level shape, for example a bare array becoming an object envelope. A
  breaking change to machine output is on the same footing as renaming a flag (R35).
- **R11.** Machine values MUST be typed by meaning:
  - an absent value is `null`, never omitted;
  - timestamps are RFC 3339 with an explicit offset;
  - durations are integers with the unit in the field name (`duration_ms`);
  - counts are numbers;
  - enum-like values are their canonical lowercase token.

  Display formatting and casing belong to the human renderer.
- **R12.** stdout MUST carry only the payload. Progress, warnings, counts, pagination
  hints, empty-state prose, and diagnostics go to stderr in every mode. Progress
  indicators appear only when attached to a terminal, and never in a machine mode.
- **R13.** A closed stdout (`EPIPE`, as in `tool list | head -1`) MUST end the process
  silently with exit code `0`: no panic and no error message.
- **R34.** A list that returns fewer records than matched, because of a default or
  explicit limit, MUST say so explicitly in its machine payload: an object envelope
  carrying the records plus `total` (the number of matches, or `null` if unknown) and
  `truncated` (a boolean), for example `{"tasks": [...], "total": 133, "truncated": true}`.
  Choose that shape when the command is first designed, because switching a shipped bare
  array to an envelope later is a breaking change (R10). An NDJSON stream has no envelope,
  so its truncation signal is the stderr notice R12 already requires in every mode. A
  list with no limit returns every match and complies with a bare array.

### Tables

- **R14.** Human tables MUST be borderless, with one header row and exactly one line per
  record. Cells never wrap. Columns are separated by two-space gutters. Numbers are
  right-aligned, with the unit in the header (`DURATION (ms)`) rather than in every cell.
  An absent cell renders as `-`, never as blank. The number of body lines equals the
  number of records.
- **R15.** A value cut to fit its column MUST end in a single `…`. Truncation MUST apply
  only to the human terminal table, never to piped or machine output. Every column that
  can be truncated MUST be retrievable in full, through a detail command (`show`) or the
  machine mode. The width to fit comes from the one resolver in R17. When no width is
  known, the table MUST NOT truncate at all. It never assumes a default such as 80
  columns.
- **R16.** A query that matches nothing MUST exit `0` and print one line to stderr naming
  what was searched. In the human and piped forms, stdout is empty. In `--json` the one
  document is still emitted: `[]` for a list, or the R34 envelope with an empty record
  array. In NDJSON nothing is written, because zero records is an empty stream. A list
  command's shape MUST NOT depend on how many records match: a list of one is still a
  table (and still a JSON array), not a key-value view. This rule governs list-shaped
  output. A `show` detail command (R2) is not a list of one, and its human view MAY be a
  key-value layout.

### Color and TTY

- **R17.** Whether to emit color MUST be decided in exactly one place. Color is off when
  stdout is not a terminal, when `TERM=dumb`, or when `NO_COLOR` is set to a non-empty
  value. A non-empty `CLICOLOR_FORCE` turns it on for a terminal. The terminal width used
  for truncation (R15) is resolved in the same place. No command body may query TTY state,
  terminal width, or these variables itself. Minimum compliant form for a clap CLI:
  - build clap without its `color` feature (on by default) and without `wrap_help` (opt-in),
    because both make clap query the terminal on its own, and leaving them out also keeps
    help goldens (R24) identical across machines;
  - take the width from `COLUMNS` alone, or from `COLUMNS` with a terminal query
    (`TIOCGWINSZ`, or the `terminal_size` crate) as the fallback, and treat "no width"
    as "do not truncate". Most shells do not export `COLUMNS`, so a `COLUMNS`-only CLI
    will usually not truncate, which is compliant.
- **R18.** Color MUST be semantic and MUST NOT be the only way meaning is carried:
  - a small closed set of roles (ok, warn, error, active, muted, neutral) is mapped from
    domain values in one table;
  - an unmapped value renders as neutral;
  - roles apply to cells, not whole rows;
  - only the basic 16-color palette is used.

  With every escape stripped, the output loses emphasis but no information.

### Errors and exit codes

- **R19.** Errors MUST go to stderr in every mode. In a machine mode an error is one JSON
  object with at least `error` (the message) and a stable `snake_case` `code`. Otherwise,
  the first line is `error: <message>`. Further lines MAY follow it for usage or a tip, as
  clap prints them. A usage error that the parser raises before the output mode can be
  resolved MAY use that plain form in every mode, provided it exits `2` (R20). A CLI that
  wants JSON usage errors pre-scans the raw arguments (and the environment rung of R8)
  for the mode before parsing.
- **R20.** Exit codes MUST be `0` for success, `1` for a command failure, and `2` for a
  usage error (bad flag, unknown subcommand, or invalid value). A command that reported an
  error MUST NOT exit `0`. Any further code MUST be documented in that command's help.
- **R21.** An error message MUST be actionable. It names the input that was rejected and,
  where the fix is known, states it: `pass --confirm to proceed`, a did-you-mean
  suggestion, or the list of valid values.

### Help and public-surface stability

- **R22.** Every command and every flag MUST have help text that says what it does. A
  flag's help states its default and its allowed values. A command whose use is not
  obvious SHOULD end its help with an `Examples:` section.
- **R23.** User-facing text MUST NOT contain internal tracker identifiers (task, issue,
  friction, or decision-record numbers) or real record IDs. User-facing text means help,
  examples, error messages, output prose, and tool or MCP descriptions. Use placeholders
  such as `<id>` or `FYYYY-MM-NNN`. Code comments, commit messages, and design docs may
  cite tracker IDs.
- **R24.** The public surface MUST be golden-tested. The rendered `--help` of every command
  and the machine output of representative commands are checked-in fixtures, compared byte
  for byte in CI. They are regenerated only through an explicit update switch, and the
  diff is reviewed in the PR. Fixtures MUST be captured from the surface as it ships: the
  built binary, or the fully assembled parser including anything grafted on at startup.
- **R25.** Each command, flag, and help string MUST be declared in exactly one place. The
  parser, `--help`, and every other surface (JSON schema, MCP or tool definition, generated
  docs) are derived from that declaration. No flag name or help string is copied by hand
  into a second place.
- **R35.** Command names, flag names, accepted values, and config keys are public surface.
  Renaming or removing one, or changing what it means, is a breaking change. A removed or
  renamed flag or config key MUST stay accepted for at least one release, either as an
  alias of its replacement or as an ignored no-op. Each use prints a stderr warning that
  names it and its replacement, if there is one. After that release, it is rejected as a
  usage error (exit `2`) or a config load error naming the key. It is never silently
  ignored. The removal is recorded in the changelog.
- **R36.** Help, schemas, tool definitions, and docs MUST advertise only commands,
  parameters, and values that a runtime path actually implements. A parameter that is
  advertised but not wired through to the code that should act on it is a defect, not a
  placeholder. Where a surface is derived from a declaration (R25), a test proves that
  each advertised operation, and each parameter, reaches its implementation. Minimum
  compliant form for a small CLI: clap derive, with every field read by the command body
  (an unused field shows up in review, or as dead code), and no hand-written help that
  names flags the parser lacks.

## Why

- **R1–R2 (grammar).** Noun-verb grammar makes the command tree predictable: once a user
  knows `task list`, they can guess `friction list` and `task show`. A shared verb
  vocabulary means agents can guess commands correctly instead of reading help for each
  noun. The `list`/`show` pairing is also what makes truncation safe (R15).
- **R3 (flag spelling).** Every alternate spelling is something a user has to remember and
  an agent can get wrong. Colliding long flags only fail when clap builds the command, so
  Orbit asserts the whole tree once in a test (ORB-11765) instead of discovering the
  collision in an unrelated parser. `--json` is sanctioned as the one shorthand because it
  predates `--format` in most CLIs, and scripts already depend on it.
- **R4 (global flags).** Context and output mode apply to every command. Declaring them per
  command is how 86 of 150 Orbit argument structs came to carry their own independent
  `--json` before the global `--format` existed. Orbit's `audit export --format` names an
  export file type, not an output mode. It is a grandfathered exception and needed special
  handling in `install_format_arg` to avoid a downcast panic, which shows why R4 forbids
  reusing a name.
- **R26 (argument ids).** clap merges a subcommand argument into a global one when they
  share an id, and nothing warns. Orbit's `workspace remove <WORKSPACE>` positional was
  captured by the global `--workspace` selector. The documented recovery for a deleted
  checkout was therefore refused for every selector form, and the remove code was
  unreachable (ORB-12169, F2026-09-133). A debugging session lost about 25 minutes in files
  that were not involved. `task export/import/reindex` each declared a `--workspace` that
  the global flag consumed, so their documented task-registry ids were always rejected
  (ORB-12134). clap's `debug_assert` catches colliding long names but not this merge,
  which is why the rule has to name it.
- **R5 (confirmation).** A prompt hangs an agent or a CI job, and a stdin read in a pipe
  consumes the pipe's data. A consistent `--confirm` is safe for scripts and easy to
  discover. Orbit's design pattern records that a second spelling (`--yes`) exists only as
  a compatibility alias.
- **R27 (EOF at a prompt).** A prompt loop that reads EOF as an empty answer re-prompts
  forever: `orbit init` spun without end when stdin was closed (ORB-11595). Orbit's
  destructive-command pattern (`docs/design-patterns/command.md:19-27`) removes prompts
  where it matters most. R27 covers the prompts that remain. It is a SHOULD because a CLI
  with no prompts has nothing to do.
- **R28 (honor every override).** An override that is accepted but ignored is worse than
  one that is rejected. The caller believes they are scoped, so they carry on. `orbit init
  --root <custom>` re-linked the operator's global skill directories (ORB-10563, an
  incident). F2026-07-138 asks for `ORBIT_ROOT` to be a hard boundary on what init may
  touch. The same class recurred across ORB-10731, ORB-11165, ORB-11388, ORB-10928,
  ORB-11417, ORB-12129, ORB-12208 and ORB-13005. It also covers selectors that were
  accepted and then ignored (ORB-13022), `mcp init/remove` bypassing the registry
  (ORB-12121), and `update --root` still replacing the host's binary (ORB-12586). The
  precedent for refusing is `orbit mcp serve`, which rejects `--root` outright instead of
  half-honoring it.
- **R29 (no silent coercion).** A write that drops a field it accepted tells the caller
  their intent was recorded when it was not. Examples: retired `task.add` fields were
  stripped silently (ORB-11698), `--fields` returned `{}` (ORB-12205), and an operator's
  `xhard` complexity was quietly demoted (ORB-12622). ORB-11666 is another case.
  F2026-07-110 states the rule: "An unknown argument to a write surface should be an
  error, not a shrug." The incident form is F2026-07-056: a PR-gated repository silently
  fell back to local shipment. F2026-07-080, F2026-08-070 and F2026-08-048 are the same
  class. Orbit's dashboard settled on all-or-nothing rejection of unsupported body fields
  (`codebases/orbit/docs/design/user-interface/4_decisions.md:231`). Rendering unknown or
  failed values honestly, rather than as zero or success, is STD-02's side of the same
  principle.
- **R30 (success means the effect happened).** `tool enable` reported success on tools
  whose availability is fixed at registration, so nothing changed (ORB-12122). `run show`
  stayed silent about tasks that were explicitly shipped but excluded (ORB-12133). Setup
  wrote config paths the client never reads (ORB-12149), and ORB-12223 is another case.
  `mcp init/remove` never named the checkout they wrote, so a wrong resolution was
  invisible (ORB-12132). Naming the resolved target turns a mis-scoped write into
  something a human or agent can see immediately.
- **R31 (reads never write).** A read that writes breaks read-only sandboxes, races real
  writers, and changes state as a side effect of looking at it. Every `orbit` command
  once rewrote the global managed-asset manifest unconditionally (ORB-10713, fixed by
  ORB-10732's no-op-when-unchanged rule). Run-failure scans finalized orphaned runs and
  released their reservations just by reading them (ORB-12941, ORB-12943). A read failed
  on a read-only path inside an executor sandbox (F2026-08-060), and a run failed before
  implementation for the same reason (F2026-09-143).
- **R32 (identifiers round-trip).** An id the tool prints but will not accept back forces
  the caller to translate between forms, and an agent will not know that it needs to.
  The host-qualified selectors that MCP hands out were rejected by `tool run` (ORB-13021).
  `config show` listed a key that `config get`/`set` refuse (ORB-12339). A task-registry
  id printed by `task import` was refused by every `--workspace` (ORB-12134).
- **R33 (no hidden filters, filter before limit).** A hidden default filter makes a
  record look missing when it is not. Orbit's `task list` used to show only
  `backlog,in-progress` by default, until ORB-10310 made it status-neutral, recent-first
  and bounded, with filters applied before the limit. F2026-07-111 is the other half: a
  state filter applied client-side over an already-limited window under-reported running
  runs.
- **R6 (one payload).** When the JSON and human views are built separately in the same
  function, they drift. In Orbit, `tool list` emitted seven JSON fields but five table
  columns. ORB-10228 added provenance fields to the audit JSON and left the printed line
  untouched. Deriving the human view from the payload makes this kind of disagreement
  impossible by construction. A small CLI can comply with a `struct` that derives
  `Serialize` plus a `fn render_human(&T)`; Orbit's `Payload`/`CommandOutput` types are one
  implementation.
- **R7–R8 (modes).** Scripts and agents need a flag that always yields parseable output.
  Resolving the mode centrally means precedence is defined once instead of per command.
  The environment rung (`ORBIT_FORMAT`) lets a whole session opt in, and falling back on a
  bad value keeps one exported typo from breaking every command in that shell. Refusing a
  `--json --format table` conflict follows R28: one of two explicit inputs would
  otherwise be silently ignored. The "check after parse" note comes from building the Rust
  CLI template against v1 (ORB-13117).
- **R9 (piped form).** Before ORB-10567 and ORB-10570, a piped `orbit tool list` received
  box glyphs, wrapped multi-line rows, and ANSI escapes, so `grep` returned fragments.
  Orbit's headerless tab-separated plain form comes from `gh`'s behaviour off a TTY. It
  also never suppresses uniform columns: ORB-12113 found that dropping one silently shifted
  every later `cut -f` field.
- **R10–R11 (payload contract).** Consumers index machine output by key. A renamed field
  breaks them silently, which is worse than a renamed flag, because a flag at least errors.
  A shape change breaks them the same way. `orbit task list --json` was silently changed
  from a bare array to a `{tasks, total, truncated}` envelope, which broke the frozen
  per-command contract, and ORB-12198 reverted it. `null` instead of omission means "not
  applicable" and "missing" read the same way. Typed values keep display formatting out
  of the contract.
- **R12 (stdout is the payload).** Prose mixed into stdout turns into a phantom record for
  every consumer. Orbit prints hints such as `showing 50 of 133 tasks …` on stderr, so
  `--json | jq` and `| wc -l` stay correct. ORB-10570 moved the JSON error object from
  stdout to stderr for the same reason. That move was a deliberate breaking change,
  announced in the changelog. Human rows printed before JSON (ORB-10791) and unparseable
  `doctor --fix-* --format json` stdout (ORB-11597, ORB-11596) are the same defect.
- **R13 (EPIPE).** `tool list | head` is normal use. Rust's `println!` panics on a closed
  pipe, and the resulting panic text looks like a crash to the user.
- **R34 (explicit truncation).** A silently capped page looks exactly like a complete one.
  Orbit shipped silent truncation twice. The MCP `task.list` returned a bare array capped
  at 50 while the CLI had gained a notice (ORB-12195). Then the ORB-12198 revert removed
  the only machine truncation signal and left `--json` callers with a capped page and no
  notice (ORB-12203). The terminal-interface decisions state the principle: "The payload
  is the contract" (`codebases/orbit/docs/design/terminal-interface/4_decisions.md:44`,
  `:57`). Putting `total`/`truncated` in the payload, in a shape chosen up front, avoids
  both failures.
- **R14–R16 (tables).** The borders existed only to separate wrapped rows (ORB-10567). Once
  a row is a single line, a border is noise to `grep` and `awk`. Cutting a value with no
  `…` and no way to recover it causes silent data loss, which is why truncation always
  comes with a detail command. Guessing a width for a sink that has none truncates output
  that nothing will display at that width. Keeping empty-state prose off stdout means a
  pipe receives an empty stream, not a sentence. `[]` in `--json` keeps "one document"
  (R7) true for zero records. A shape that stays the same regardless of record count means
  a consumer never needs to branch on the count. The `show` carve-out resolves v1's
  ambiguity with R2 and R15, which define `show` as the one-record detail command. That
  ambiguity was raised by the constellation-standards skill author.
- **R17 (one color gate).** Orbit's two styling crates each ran their own `NO_COLOR` and
  TTY checks, and they disagreed: `comfy_table` ignored `NO_COLOR` and wrote escapes into
  redirected files (ORB-10570). One decision point, which overrides every backend, is the
  only arrangement that stays correct as more backends are added. The `NO_COLOR`
  convention (no-color.org) is the baseline users expect. clap's `color` and `wrap_help`
  features are exactly such a second backend, which is why the minimum form leaves them
  out. That lesson comes from the template build (ORB-13117).
- **R18 (semantic color).** Orbit's status palette was defined twice, and the two copies
  drifted: `backlog` was colored in one and not the other (T20260427-43, ORB-10202). A
  closed role set bounds the palette, so it can be audited for contrast. Because the word
  always prints, `NO_COLOR` and piped output are correct, not degraded.
- **R19–R20 (errors).** Scripts should branch on the exit code and the stable `code` value,
  never on message wording. Following clap's convention of exit code `2` for usage errors
  lets a wrapper tell "you called it wrong" from "it ran and failed". Allowing clap's
  multi-line usage error, and a plain usage error when the mode cannot yet be known,
  matches what a parser can actually do before it has succeeded (ORB-13117). The exit
  code still carries the machine-readable fact. Error typing and the mapping to codes at
  crate boundaries belong to STD-02.
- **R21 (actionable errors).** Every error message that names the fix saves a round trip
  through `--help`, for a human and, more expensively, for an agent.
- **R22 (help).** For agents, help is the primary way to discover how a command works.
  Examples show real invocation shapes faster than a list of flags.
- **R23 (no internal IDs).** A tracker ID in help or output means nothing to an outside
  user and goes stale when the tracker moves. Real record IDs in examples leak one
  workspace's data into every other workspace. This is a standing rule in Orbit's
  `AGENTS.md`, and Orbit enforces it with a recursive scan of the help tree.
- **R24 (goldens).** Help and machine output are contracts, and in Orbit they drift through
  incidental causes: a clap upgrade, reordered fields, a new global flag. A byte-for-byte
  fixture turns "wire compatible" from a claim in review into a check (ORB-10358 froze the
  friction help before migrating it; ORB-10571 added the output goldens). Capturing from
  the shipped surface matters: Orbit's help goldens render from the derive tree, before
  `main` grafts on `--format`, so they do not match what users see (see Exemplars).
- **R25 (declare once).** Orbit's friction noun was once written out on four surfaces: CLI,
  MCP, dashboard, and runtime. `show` and `update` described the same `id` field in two
  different ways (ORB-10358). For a small CLI, clap derive already satisfies R25, since the
  struct is the single declaration and help comes from its doc comments. Orbit's
  `OperationSpec` registry is what R25 looks like once several surfaces (CLI and MCP) have
  to agree.
- **R35 (deprecation window).** A flag or key that vanishes between releases breaks every
  script and config file that used it, all at once. One that is silently ignored forever
  hides that the caller's intent no longer does anything. Orbit settled on the same
  migration three times: the routines `hosts:` key is "accepted and ignored with a load
  warning for one release, then rejected"
  (`codebases/orbit/docs/design/routines/4_decisions.md:250`). The retired
  `owner_host_ids` catalog key was dropped for one release instead of failing every
  command (`…/host-registry/4_decisions.md:201`). Removed operation-mode keys are warned
  about by name and ignored at load (`…/orbit-core/4_decisions.md:191`). v1 said a flag
  rename was breaking only by analogy to a field rename (R10). The skill author asked for
  it to be explicit.
- **R36 (advertise only what runs).** An advertised capability that does nothing is a
  trap for agents, who discover commands through help and schemas. A shipped asset
  described executor behaviour that no runtime implemented
  (`codebases/orbit/docs/design/executors/4_decisions.md:69`). An operator-only tool
  shipped without its CLI subcommand is "an unreachable surface, which is what happened
  here" (`…/orbit-core/4_decisions.md:147`). A feature shipped with no reachable caller
  (F2026-08-005). An advertised `state` filter never reached the backend, and no test
  checked that it did (F2026-07-111). ORB-12581 is another case.

## Exemplars

Paths are relative to the constellation root. Live `orbit` invocations were checked
against orbit 0.24.0 on 2026-09-26. Exemplar paths for R26–R36 were checked against
`codebases/orbit` at `05d32aaac` on the same date.

- R1 — `codebases/orbit/crates/orbit-cli/src/command/mod.rs#Commands`, and live
  `orbit task --help` (noun `task`, verbs grouped under `Tasks:`/`Health:`/`Bundles:`).
- R2 — `codebases/orbit/docs/design/terminal-interface/references/detail-commands.md#Covered`
  (each `<noun> list` paired with `<noun> show`).
- R3 — `codebases/orbit/crates/orbit-cli/src/command/task/add.rs` line 29
  (`#[arg(long = "tag", action = ArgAction::Append, …)]`), and
  `codebases/orbit/crates/orbit-cli/src/command/tests/mod.rs#cli_command_tree_debug_assert_rejects_duplicate_long_flags`.
- R4 — `codebases/orbit/crates/orbit-cli/src/command/mod.rs#Cli` (`--root`, `--workspace`
  with `global = true`), `codebases/orbit/crates/orbit-cli/src/main.rs#install_format_arg`,
  and `codebases/orbit/crates/orbit-cli/src/tests/cli_format.rs#format_is_accepted_before_and_after_the_subcommand`.
- R26 — `codebases/orbit/crates/orbit-cli/src/command/workspace/remove.rs#WorkspaceRemoveArgs`
  and `…/command/workspace/teardown.rs` (`#[arg(value_name = "WORKSPACE", id =
  "workspace_selector")]`), with the behaviour regression
  `codebases/orbit/crates/orbit-cli/tests/workspace_selector.rs#workspace_remove_deregisters_deleted_checkout_by_name_id_and_path`.
- R5 — `codebases/orbit/crates/orbit-cli/src/command/mod.rs#require_confirmation`, and
  `codebases/orbit/docs/design-patterns/command.md#Destructive CLI confirmation`.
- R27 — `codebases/orbit/crates/orbit-cli/src/command/init/prompt_stdin.rs#STDIN_CLOSED_BEFORE_PROMPT`
  (the message names `--task-prefix`/`--machine-name` and `--non-interactive`) and
  `#NON_TTY_PROMPT_TIMEOUT`, plus
  `codebases/orbit/crates/orbit-cli/src/command/init/tests/command.rs#task_prefix_prompt_rejects_closed_stdin_without_retrying`.
- R28 — `codebases/orbit/crates/orbit-cli/src/command/mcp/command.rs#ServeArgs::execute_without_runtime`
  (refuses a root override: "does not accept a workspace root override"),
  `codebases/orbit/crates/orbit-cli/tests/update.rs#update_preserves_explicit_and_environment_roots_from_another_checkout`,
  and `codebases/orbit/crates/orbit-cli/tests/workspace_selector.rs#migrate_dry_run_honors_selected_checkout_and_confirm_uses_the_same_one`.
- R29 — `codebases/orbit/crates/orbit-common/src/protocol/tool_input.rs#reject_retired_task_add_input_fields`
  (names the field and the tool that accepts it),
  `codebases/orbit/crates/orbit-tools/src/builtin/orbit/task/tests/strict_input.rs#add_rejects_unknown_key`,
  and `codebases/orbit/docs/design/user-interface/4_decisions.md#All-or-Nothing Rejection of Unsupported Task Body Fields`.
- R30 — `codebases/orbit/crates/orbit-cli/src/command/mcp/setup/dispatch.rs#format_action_summary`
  (`mcp init: claude -> <checkout> (<ws id>)`),
  `…/mcp/setup/tests/dispatch.rs#action_summary_names_the_resolved_checkout_and_workspace_id`,
  and `codebases/orbit/crates/orbit-core/src/adapter/command/registry.rs#set_tool_enabled_state`
  (refuses rather than reporting a no-op success).
- R31 — `codebases/orbit/crates/orbit-cmd/src/registry/runtime/factory.rs#initialize_read_only_with_overrides`,
  `codebases/orbit/crates/orbit-common/src/storage/tests/sqlite.rs#private_read_only_filesystem_path_has_no_side_effects`,
  and `codebases/orbit/crates/orbit-cli/tests/mcp_roundtrip.rs#read_only_registry_files_keep_uncheckpointed_wal_reads_observational`.
  Partial: `codebases/orbit/crates/orbit-cli/src/command/run/steps.rs#RunRead::from_no_reconcile`
  (see Deviations).
- R32 — `codebases/orbit/crates/orbit-cmd/src/registry/runtime/tests/selection.rs#local_host_qualified_selector_binds_and_foreign_host_fails_closed`,
  and `codebases/orbit/crates/orbit-config/src/layering.rs#effective_values` (the listed
  settings come only from the key registry, so every listed key is `get`/`set`-admitted).
- R33 — `codebases/orbit/crates/orbit-cli/src/command/task/list.rs` (`--status` is opt-in;
  `--limit` defaults to `DEFAULT_TASK_LIST_LIMIT` and applies after filters), and
  `codebases/orbit/crates/orbit-cli/tests/task_list.rs#older_someday_task_discoverable_ahead_of_50_newer_done_tasks`.
- R6 — `codebases/orbit/crates/orbit-cli/src/output/payload.rs#Payload`,
  `codebases/orbit/crates/orbit-cli/src/output/render.rs#emit`, and
  `codebases/orbit/docs/design/terminal-interface/4_decisions.md#Terminal Output Is a Rendering of a Structured Payload`.
- R7 — `codebases/orbit/crates/orbit-cli/src/main.rs#format_arg`
  (`auto|table|json|ndjson`), and `codebases/orbit/crates/orbit-cli/src/output/render.rs#ndjson_records`.
- R8 — `codebases/orbit/crates/orbit-cli/src/output/sink.rs#resolve_mode`.
- R9 — `codebases/orbit/crates/orbit-cli/src/output/table.rs#render_plain`, and
  `codebases/orbit/crates/orbit-cli/tests/output_goldens/task_list.plain.txt`.
- R10 — `codebases/orbit/crates/orbit-cli/tests/json_output_stability.rs#json_flag_output_is_untouched_by_the_global_format_machinery`,
  and `codebases/orbit/docs/design/terminal-interface/specs/output-modes.md#4. Payload Rules`.
- R11 — `codebases/orbit/docs/design/terminal-interface/specs/output-modes.md#4. Payload Rules`.
  Live check: `orbit task show <id> --json` carries `"parent_id": null` and
  `"created_at": "…+00:00"`.
- R12 — `codebases/orbit/crates/orbit-cli/src/output/table.rs#trailing_notice`,
  `codebases/orbit/crates/orbit-cli/tests/task_list.rs#task_list_truncation_notice_reaches_stderr_in_json_and_ndjson_modes`,
  and `codebases/orbit/crates/orbit-cli/src/output/sink.rs#progress_allowed`.
- R13 — `codebases/orbit/crates/orbit-cli/src/output/pipe.rs#install_handler`.
- R34 — `codebases/orbit/crates/orbit-core/src/adapter/tool_host/task_tools.rs#list`
  (the `orbit.task.list` tool returns `{tasks, total, truncated}`), and
  `codebases/orbit/crates/orbit-core/src/adapter/tool_host/tests/task_tools.rs#task_list_tool_is_status_aware_and_bounded`.
  The CLI's grandfathered bare array is covered under Deviations.
- R14 — `codebases/orbit/crates/orbit-cli/src/output/table.rs#add_row` (the only row
  constructor, height capped at 1), `…/output/table.rs#Column::number`, and
  `codebases/orbit/docs/design/terminal-interface/specs/table-rendering.md`.
- R15 — `codebases/orbit/crates/orbit-cli/src/output/table.rs#Column::path` (middle
  truncation for identifying tails),
  `codebases/orbit/docs/design/terminal-interface/references/detail-commands.md`, and
  `codebases/orbit/crates/orbit-cli/src/output/tests/sink.rs#absent_width_disables_truncation_rather_than_defaulting_to_eighty`.
- R16 — `codebases/orbit/crates/orbit-cli/tests/table_rendering.rs#a_list_with_no_matches_leaves_stdout_empty_and_explains_itself_on_stderr`,
  `codebases/orbit/crates/orbit-cli/tests/json_output_stability.rs#empty_list_json_is_exactly_an_empty_array`,
  and `codebases/orbit/crates/orbit-cli/src/output/table.rs#empty_message`.
- R17 — `codebases/orbit/crates/orbit-cli/src/output/sink.rs#resolve_color`,
  `…/output/sink.rs#apply_color_policy`, `…/output/sink.rs#resolve_width` (`COLUMNS`
  preferred, then the `TIOCGWINSZ` query in `#query_terminal_width`; width `0` means "do
  not truncate"), and
  `codebases/orbit/scripts/check-terminal-state-guard.sh`.
- R18 — `codebases/orbit/crates/orbit-cli/src/output/color.rs#role_for`, and
  `codebases/orbit/docs/design/terminal-interface/specs/color-and-styling.md#3. Rules`.
- R19 — `codebases/orbit/crates/orbit-cli/src/main.rs#print_error`, and
  `codebases/orbit/crates/orbit-cli/src/output/json.rs#error_payload` / `#error_code`.
  Orbit's `main.rs#parse_cli` lets clap print its plain usage error and exit `2` in every
  mode, which is the form R19 allows when the mode is not yet known.
- R20 — `codebases/orbit/crates/orbit-cli/src/main.rs#finish_command`. Live check:
  `orbit task show <missing>` exits `1`, and `orbit task list --bogus` exits `2`.
- R21 — `codebases/orbit/crates/orbit-cli/src/command/mod.rs#require_confirmation`
  (`… is irreversible; pass --confirm to proceed`), and
  `codebases/orbit/crates/orbit-cli/src/main.rs#repair_crew_flag_suggestion`. Live check:
  `orbit task lst` prints the tip `some similar subcommands exist: 'lint', 'list'`.
- R22 — `codebases/orbit/crates/orbit-cli/src/command/mod.rs#ROOT_HELP_TEMPLATE` (grouped
  root help), and live `orbit task list --help` (possible values listed, `Examples:` block).
- R23 — `codebases/orbit/AGENTS.md#Code` ("Never expose internal task/friction IDs …"),
  and `codebases/orbit/crates/orbit-cli/src/command/tests/mod.rs#recursive_cli_help_uses_only_placeholder_artifact_ids`.
- R24 — `codebases/orbit/scripts/check-goldens.sh` (run as `make goldens`, regenerated with
  `make goldens UPDATE=1`), `codebases/orbit/crates/orbit-cli/src/command/tests/mod.rs#assert_help_matches_golden`,
  `codebases/orbit/crates/orbit-cli/tests/output_goldens.rs`, and
  `codebases/orbit/crates/orbit-cli/tests/snapshots/mcp_tools_list.json`. Counter-example
  for the "as shipped" clause: `codebases/orbit/crates/orbit-cli/src/command/tests/friction_help/show.txt`
  has no `--format` line, but live `orbit friction show --help` does.
- R25 — `codebases/orbit/crates/orbit-common/src/governance/operation.rs#OperationSpec`,
  `codebases/orbit/crates/orbit-common/src/governance/friction/operations.rs`,
  `codebases/orbit/crates/orbit-cli/src/command/operation_args.rs`,
  `codebases/orbit/crates/orbit-cli/src/command/tests/operation_args.rs` (a synthetic noun
  gets a complete CLI from nothing but a registry entry), and
  `codebases/orbit/docs/design/operations-as-data/1_overview.md`.
- R35 — `codebases/orbit/crates/orbit-config/src/registry/keys.rs#REMOVED_CONFIG_KEYS`
  with `codebases/orbit/crates/orbit-config/src/resolved.rs#warn_removed_key` (removed
  config keys are warned about by name and ignored),
  `codebases/orbit/crates/orbit-config/src/tests/layering.rs#removed_pilot_max_complexity_warns_once_for_the_layer_that_sets_it`,
  `codebases/orbit/crates/orbit-cli/src/command/tests/mod.rs#cli_parses_web_serve_global_as_deprecated_noop`
  (a deprecated flag that still parses), and
  `codebases/orbit/crates/orbit-cli/src/command/run/tests/ship.rs#ship_rejects_removed_local_subcommand_form`
  (a removed form, rejected after its window).
- R36 — `codebases/orbit/crates/orbit-tools/src/builtin/orbit/tests/search.rs#search_schema_advertises_only_lexical_inputs`,
  `codebases/orbit/crates/orbit-cli/tests/mcp_roundtrip/plugins.rs#an_enabled_plugin_tool_is_advertised_and_callable_and_a_disabled_one_is_not`,
  and `codebases/orbit/crates/orbit-mcp/src/federated/tests/capability.rs#the_locked_mapping_covers_exactly_the_advertised_surface`.

## Machine checks

The **Gate** column names what an adopting repo wires up. The **Orbit instance** column
names where Orbit already has that gate. A rule marked `review-only` has no mechanical
gate that is worth its false-positive rate, so reviewers check it against the rule text.
For a new Rust CLI, `assert_cmd` for exit codes and streams, plus `insta` or `trycmd` for
goldens, covers most of this table.

| Rule | Gate | Orbit instance |
|---|---|---|
| R1 | review-only | Root-help grouping test: `command/tests/mod.rs#root_help_groups_every_visible_command_exactly_once` |
| R2 | review-only | — |
| R3 | Whole-tree parser assertion (clap `Command::debug_assert()` in a test) catches colliding long flags; spelling consistency is review-only | `cli_command_tree_debug_assert_rejects_duplicate_long_flags` |
| R4 | Unit test: the global flag parses before and after a subcommand | `src/tests/cli_format.rs` (`format_is_accepted_before_and_after_the_subcommand`, `every_command_declares_exactly_one_format_…`) |
| R26 | Unit test that walks the command tree (`get_subcommands()` / `get_arguments()`) and fails if a subcommand argument's id equals a global argument's id | review-only in Orbit (explicit `id = "workspace_selector"`); behaviour regression `tests/workspace_selector.rs#workspace_remove_deregisters_…` |
| R5 | Test: the destructive verb without `--confirm` fails before any mutation | review-only in Orbit (shared `require_confirmation` helper) |
| R27 | Unit test: the prompt reader given empty input fails once and prompts once; binary test with closed stdin exits non-zero within a deadline | `command/init/tests/command.rs#task_prefix_prompt_rejects_closed_stdin_without_retrying`; `tests/init_interactive_stdin.rs#closed_stdin_keeps_the_existing_message` |
| R28 | Integration test per override: run under a scratch root and HOME, then assert host-global paths are byte-for-byte unchanged; a test that an unsupported override is refused | `tests/ambient_authority_isolation.rs#fixture_lifecycle_leaves_the_ambient_authority_byte_for_byte_unchanged`; `tests/update.rs#update_preserves_explicit_and_environment_roots_…` |
| R29 | Test: an unknown or unsupported field fails, names the field, and writes nothing | `orbit-tools/…/task/tests/strict_input.rs#add_rejects_unknown_key`; `orbit-web/src/api/tests/config.rs#an_unknown_key_is_refused_before_any_write` |
| R30 | Test asserts the effect by reading state back, not the success message; unit test that the success line names the resolved target | `mcp/setup/tests/dispatch.rs#action_summary_names_the_resolved_checkout_and_workspace_id` |
| R31 | Integration test: run every read command against a read-only (or digest-snapshotted) data root and assert nothing changed | `orbit-common/src/storage/tests/sqlite.rs#private_read_only_filesystem_path_has_no_side_effects`; `tests/mcp_roundtrip.rs#read_only_registry_files_keep_…`; partial (run reads reconcile by default) |
| R32 | Round-trip test: every id form the tool emits in `list --json` is fed back to `show` and to each command that takes that selector | `orbit-cmd/…/tests/selection.rs#local_host_qualified_selector_binds_and_foreign_host_fails_closed` |
| R33 | Integration test: more than N newer non-matching records plus one older match, then `--filter --limit N` returns the match; the default list spans every status | `tests/task_list.rs#older_someday_task_discoverable_ahead_of_50_newer_done_tasks`; `tests/task_list.rs#existing_filters_keep_behaviour` |
| R6 | Output goldens: the human and machine forms of the same fixture are both pinned | `tests/output_goldens.rs#plain_and_json_forms_match_their_goldens` |
| R7 | Output goldens include a `--json` form for each list and detail command; a test that `--json` with a different `--format` exits 2 | `tests/output_goldens/*.json`; `tests/json_output_stability.rs`; conflict check absent (see Deviations) |
| R8 | Unit test of the resolver's precedence table | `src/output/tests/sink.rs` (`rung_one_…` through `rung_four_…`, `unrecognized_environment_value_falls_through_to_auto`) |
| R9 | Integration test: piped output contains no `\x1b[` and no box glyphs; N records produce N lines | `tests/output_goldens.rs#no_ansi_escapes_under_any_color_configuration`; `tests/table_rendering.rs` |
| R10 | Byte-exact JSON goldens; a diff must be reviewed as a contract change | `tests/json_output_stability.rs`; `make goldens` |
| R11 | review-only (goldens catch regressions, not first violations) | — |
| R12 | Integration test: stdout is empty or pure payload, and notices land on stderr | `tests/task_list.rs#task_list_truncation_notice_reaches_stderr_…`; `src/output/tests/gating.rs#progress_needs_a_terminal_and_a_human_facing_mode` |
| R13 | Test: a broken-pipe write is treated as a silent exit 0 | `src/output/tests/pipe.rs` |
| R34 | Test with more matches than the limit: `--json` carries `total` and `truncated: true`; the stderr notice appears in every mode | `orbit-core/…/tool_host/tests/task_tools.rs#task_list_tool_is_status_aware_and_bounded`; `orbit-web/src/api/tests/tasks.rs` (`truncated` assertions); `tests/task_list.rs#task_list_truncation_notice_reaches_stderr_in_json_and_ndjson_modes` |
| R14 | Rendering unit tests at a pinned width (never read the width from the environment in tests) | `src/output/tests/table.rs`; `tests/table_rendering.rs` |
| R15 | Rendering unit test for `…`, and one that an unknown width does not truncate; detail-command coverage is review-only (keep a table of truncatable columns and their detail commands) | `src/output/tests/table.rs#overflow_is_truncated_with_a_single_ellipsis`; `src/output/tests/sink.rs#absent_width_disables_truncation_rather_than_defaulting_to_eighty`; `references/detail-commands.md` |
| R16 | Integration test: an empty result gives empty stdout (or exactly `[]` under `--json`), a stderr line, and exit 0 | `tests/table_rendering.rs#a_list_with_no_matches_…`; `tests/json_output_stability.rs#empty_list_json_is_exactly_an_empty_array` |
| R17 | Grep guard in CI: no TTY, width, or color-env query outside the one resolver module; manifest review that clap is built without `color` and `wrap_help` | `scripts/check-terminal-state-guard.sh` (run by `scripts/ci-guardrails.sh`); `src/output/tests/sink.rs#no_color_disables_color_and_outranks_clicolor_force`; `src/output/tests/gating.rs` |
| R18 | review-only | — |
| R19 | Integration test: a failing `--json` call puts `{error, code}` on stderr and nothing on stdout; a usage error's first stderr line starts with `error:` | review-only in Orbit (no CLI test pins `print_error`) |
| R20 | Integration test with explicit `.code(1)` / `.code(2)` assertions | partial: `tests/plugin_cli_group.rs` asserts `.code(1)`; code 2 is clap's default and unpinned |
| R21 | review-only | — |
| R22 | Help goldens make every help change visible in the diff; quality is review-only | `make goldens` |
| R23 | Test that renders the whole help tree recursively and rejects tracker-ID patterns | `command/tests/mod.rs#recursive_cli_help_uses_only_placeholder_artifact_ids` |
| R24 | Help and output golden suite in CI, with an explicit update switch | `make goldens` → `scripts/check-goldens.sh` (`ORBIT_UPDATE_HELP_GOLDENS`, `ORBIT_UPDATE_OUTPUT_GOLDENS`, `ORBIT_MCP_UPDATE_SNAPSHOT`) |
| R25 | review-only for a single-surface CLI; with several surfaces, a test that derives each surface from the declaration and compares it to a snapshot | `command/tests/operation_args.rs`; `tests/snapshots/mcp_tools_list.json` |
| R35 | Test: a deprecated flag or key still parses and warns by name; once its window closes, a test that it is rejected (exit 2 or a load error); help goldens (R24) show the removal | `orbit-config/src/tests/layering.rs#removed_pilot_max_complexity_warns_once_…`; `command/tests/mod.rs#cli_parses_web_serve_global_as_deprecated_noop`; `command/run/tests/ship.rs#ship_rejects_removed_local_subcommand_form` |
| R36 | Parity test: every advertised operation and parameter is invoked with a probe input and must reach its implementation; review-only for a single-surface clap CLI | `orbit-tools/…/tests/search.rs#search_schema_advertises_only_lexical_inputs`; `tests/mcp_roundtrip/plugins.rs#an_enabled_plugin_tool_is_advertised_and_callable_…`; `orbit-mcp/src/federated/tests/capability.rs#the_locked_mapping_covers_exactly_the_advertised_surface` |

## Applies to

- `cli` — R1–R36 (every rule; R26 is phrased for clap and holds trivially for a parser
  without global arguments)
- `service` — R29, R30, R32, R36 (any write surface, id-emitting surface, or advertised
  schema, such as an HTTP API or an MCP tool, not only a CLI)

## Deviations

An adopting repo that departs from a rule records a decision in its own design docs,
citing `STD-01@<version> §Rn` and the reason (for example `STD-01@2 §R4`). It never edits
its vendored copy of this standard.

Orbit carries these recorded deviations. Adopters should not copy them:

- `audit export --format` reuses the global flag's name with a different meaning (against
  R4).
- The legacy `--json` flag pretty-prints on every sink, while `--format json` pretty-prints
  only on a terminal. Orbit keeps this for byte-compatibility with older scripts.
- `--format <mode> --json` is not refused. `--format` silently outranks the legacy `--json`
  (against R7's conflict clause).
- `orbit task list --json` stays a bare array, because R10 froze its shape. Its truncation
  signal is only the stderr notice. The `orbit.task.list` tool and the dashboard API carry
  the `{tasks, total, truncated}` envelope (against R34).
- `orbit run history|show|logs|events` reconcile stale runs as a side effect of reading
  them. Observation-only reads need the opt-in `--no-reconcile` (against R31).
- Orbit builds clap with its default `color` feature, so clap colors its own help and
  usage errors outside the one gate. clap's detection follows the same `NO_COLOR` and TTY
  conventions, so the two agree in practice (against R17's minimum form).

## Changelog

- **v2 (2026-09-26):**
  - Added:
    - R26: argument ids never shadow a global argument, as a clap note (C1).
    - R27: EOF at a prompt is an error, SHOULD (C4).
    - R28: overrides are honored on every path or rejected (T1).
    - R29: caller input is never silently dropped or coerced (T3).
    - R30: success means the effect happened and names its target (C3).
    - R31: read-only commands never write (T16).
    - R32: identifiers round-trip (C2).
    - R33: no hidden filters, and filters apply before the limit (C7).
    - R34: truncation is explicit in the payload (T12).
    - R35: flag and config-key deprecation window (C5).
    - R36: advertise only what a runtime path implements (A3).
    - A new "Inputs and effects" cluster.
  - Clarified:
    - R3 and R35: renaming or removing a flag is explicitly a breaking change.
    - R3 and R7: `--json` is the one sanctioned shorthand for `--format json`, and a
      conflicting pair is a usage error checked after parse.
    - R10: a top-level shape change is breaking.
    - R15 and R17: the width comes from the one resolver, no width means no truncation,
      `COLUMNS`-only is compliant, and clap is built without `color`/`wrap_help`.
    - R16: it governs list-shaped output, a `show` detail view MAY be key-value, and an
      empty list is `[]` under `--json` and nothing under NDJSON.
    - R19: a usage error may be multi-line and plain when the mode cannot yet be
      resolved; pre-scanning the raw arguments is how to get JSON usage errors.
  - Editorial: `###` cluster headings, the shared "Applies to" form, and deviation
    examples using `STD-01@<version>`.
  - Sources: ORB-13088 (candidate disposition), ORB-13117 (template-author feedback),
    ORB-13118 (skill-author feedback).
- **v1 (2026-09-26):** initial publication, R1–R25.
