---
title: Conformance
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
type: design
summary: Every rule of each adopted constellation standard, marked complies, deviation (with its decision), n/a or gap (with its task).
tags: [standards, conformance]
related_artifacts: [ORB-13138]
---

# Conformance

pulsar adopts STD-01@2 to STD-05@1 (vendored in [docs/standards](../standards/)). This page
marks every rule:

- **Complies**: the code does what the rule asks; the evidence column names where.
- **Deviation**: a recorded decision explains why not, linked.
- **N/A**: the rule's subject does not exist in pulsar.
- **Gap**: not met yet; the task that closes it is named.

A change that alters a row updates it in the same commit. Reviewed against the code on the
`last_validated` date in [ORB-13138] (the standards alignment).

## STD-01@2 — CLI surface

| Rule | Status | Evidence |
|---|---|---|
| R1, R2 | Deviation | [verb-first grammar](./surfaces/4_decisions.md#the-cli-keeps-its-verb-first-grammar) |
| R3 | Complies | long kebab-case flags, one spelling per concept (`surfaces/cli/commands/`) |
| R4 | Gap | `--account` is per subcommand, not global: [ORB-13210] |
| R5 | Deviation | `--confirm` on `publish`, `auth logout`, `import-posted`, `migrate`, `auth migrate`; [reconcile and first-use migration](./surfaces/4_decisions.md#reconcile-and-first-use-migration-apply-without---confirm) |
| R6, R7 | Complies | one payload per command, every rendering derived from it (`surfaces/cli/views.py`); `--format`/`--json` anywhere, conflict refused |
| R8 | Complies | flag > `PULSAR_FORMAT` > auto, resolved once (`surfaces/cli/render.py::resolve_mode`) |
| R9 | Complies | piped: tab-separated lines, no header, escapes or truncation (`tests/test_render.py`) |
| R10 | Deviation | [fields renamed before the first release](./surfaces/4_decisions.md#cli-output-fields-renamed-before-the-first-release) |
| R11, R12, R13 | Complies | typed values; stdout carries only the payload; `EPIPE` ends quietly (`surfaces/cli/main.py`) |
| R14 | Complies | borderless tables, one line per record, `-` for absent, right-aligned numbers (`surfaces/cli/render.py::render`) |
| R15, R16 | Complies | `…` only for a known width, full values in `--json`; an empty list prints nothing on stdout (JSON: `writes: []`) and a stderr notice, exit 0 |
| R17 | Deviation | one decision point (`surfaces/cli/render.py::resolve_terminal`), except argparse wrapping `--help`: [renders for its reader](./surfaces/4_decisions.md#the-cli-renders-for-its-reader-as-orbits-does) |
| R18 | Complies | one role table (`surfaces/cli/render.py::ROLES`), cells only, 16 colors; stripping escapes loses nothing (test) |
| R19 | Complies | `error: <message>` on stderr, or one JSON object in JSON mode; usage errors pre-scan argv and `PULSAR_FORMAT` (`surfaces/cli/main.py::main`) |
| R20 | Deviation | [`orbit-tool` exits 0 when it answered](./surfaces/4_decisions.md#pulsar-orbit-tool-exits-0-when-it-answered); other commands use 0/1/2 |
| R21 | Complies | messages name the input and a remedy pinned to the home (`login_command`, `home_command`) |
| R22, R23 | Complies | every command and flag has help; no task ids in user text |
| R24 | Complies | help and output goldens in `tests/goldens/` |
| R25 | Gap | plugin schemas and MCP descriptions are hand-kept beside the CLI: [ORB-13211] |
| R26 | Complies | distinct `dest` names per parser |
| R27 | N/A | no prompts |
| R28, R29, R30 | Complies | unknown flags refused; `--limit` refused outside 1–100, never clamped; success only after the effect |
| R31 | Complies | reports open the home read-only and migrate nothing (`Runtime(read_only=True)`) |
| R32, R33, R34 | Complies | printed ids are usable as input; no implicit filters; `history` reports `total` and `truncated` |
| R35 | Deviation | [renamed flags stay for one release](./surfaces/4_decisions.md#renamed-flags-stay-for-one-release); [fields renamed before the first release](./surfaces/4_decisions.md#cli-output-fields-renamed-before-the-first-release); changelog: [ORB-13216] |
| R36 | Complies | help, schemas and docs advertise only what exists (`tests/test_docs.py` parses documented commands) |

## STD-02@2 — Architecture and errors

| Rule | Status | Evidence |
|---|---|---|
| R1 | Complies | [ARCHITECTURE.md](./ARCHITECTURE.md); `tests/test_layering.py` |
| R2 | Gap | MCP `upload_media`/`delete_post` claim and settle in the surface; CLI auth verbs build the registry: [ORB-13212] |
| R3 | Complies | core takes environment, cwd and home as arguments; `expanduser`/`cwd` banned in core (`test_layering.py`) |
| R4 | Complies | mechanisms (`fsutil`, `guard`, `ledger`) import no feature |
| R5 | Gap | no I/O-free contract module: [ORB-13212] |
| R6 | Gap | direction is checked by `make check`, but there is no CI: [ORB-13214] |
| R7, R9 | Complies | one package; dependencies declared once in `pyproject.toml` |
| R8 | Gap | `core/ledger/__init__.py` exports more than other packages use: [ORB-13213] |
| R10 | Deviation | [X's duplicate refusal is classified from its text](./publishing/4_decisions.md#xs-duplicate-refusal-is-classified-from-its-text) |
| R11, R12, R13 | Complies | one `PulsarError` with codes, translated once per surface; no asserts on fallible paths |
| R14 | Deviation | [protocol strings validated at the edge](./publishing/4_decisions.md#protocol-strings-are-validated-at-the-edge-not-wrapped-in-types) |
| R15 | Gap | `providers/x/auth.py` writes prompts itself: [ORB-13212] |
| R16 | Gap | a token response without `expires_in` is stored as expired now: [ORB-13213] |
| R17 | Complies | unused state constants removed |
| R18 | Complies | no source file over ~800 lines |
| R19, R20 | N/A | Rust test-layout mechanics |
| R21 | Complies | tests run the code; the launcher is exercised by `tests/test_launcher.py` |
| R22 | Gap | ruff baseline without T20 (no `print` in `src`): [ORB-13214] |
| R23 | Deviation | [no supply-chain gate beyond the lock and dependabot](./surfaces/4_decisions.md#no-supply-chain-gate-beyond-the-lock-and-dependabot); gate: [ORB-13208] |
| R24 | Complies | one `is_ambiguous`, one `check_key`, one `check_limit` (the CLI's parse-time check uses its bound) |
| R25 | Complies | the ledger, scanner and policy are code, not prompt text |
| R26 | Complies | remedies name the resolved home (`PULSAR_HOME=… pulsar …`) |
| R27 | Gap | account status and migration state are strings, not enums: [ORB-13213] |
| R28–R34 | Complies | config validated at load; unknown health is `unverified`; distinct states for wait, dead end and unknown; integrity fails closed; one bad row isolated; recovery accepts `submitting`/`unknown`; preconditions before the first write (`_publish_preview`, `prepare`) |

## STD-03@2 — Concurrency and process safety

| Rule | Status | Evidence |
|---|---|---|
| R1 | Deviation | [publisher ledger writes run on the event loop](./publishing/4_decisions.md#publisher-ledger-writes-run-on-the-event-loop); other blocking I/O goes to a thread (`tests/test_runtime.py`) |
| R2 | N/A | no channels or queues |
| R3–R7 | Complies | documented lock order (`core/accounts.py`); `finally` releases; one atomic-write helper; `flock` with bounded waits naming the holder |
| R8 | Deviation | [`writes.jsonl` is a best-effort export](./publishing/4_decisions.md#writesjsonl-is-a-best-effort-export) |
| R9 | Complies | a `submitting`/`unknown` row is settled only from the timeline, by compare-and-set |
| R10 | Complies | newer ledgers refused; a newer `accounts.json` is read only when its `min_reader_version` names this version |
| R11 | Deviation | [the browser is opened, not supervised](./accounts/4_decisions.md#the-browser-is-opened-not-supervised) |
| R12 | Complies | the launcher's `uv sync` runs under `timeout -k 10 600` |
| R13 | Deviation | [the launcher's sync bound](./surfaces/4_decisions.md#the-launchers-sync-is-bounded-only-where-timeout1-runs): no group sweep after a clean exit |
| R14 | Complies | pulsar never signals a PID |
| R15 | Gap | X error bodies reach `detail` unbounded: [ORB-13215] |
| R16 | Deviation | [the browser is opened, not supervised](./accounts/4_decisions.md#the-browser-is-opened-not-supervised) |
| R17, R18 | Complies | multiprocessing tests fail fast on a dead child and always reap (`tests/conftest.py::collect`, `reap`) |
| R19 | N/A | no self re-exec |
| R20 | Complies | `hermetic_env` isolates HOME, XDG and PULSAR_HOME per test |
| R21 | Gap | no CI job or runner timeout contains the test run: [ORB-13214] |
| R22 | Deviation | [the launcher's sync bound](./surfaces/4_decisions.md#the-launchers-sync-is-bounded-only-where-timeout1-runs); the login callback drops silent connections; open: upload deadline [ORB-13207], HTTP server bounds [ORB-13216], test timeout [ORB-13214] |
| R23, R24 | Complies | append-only migrations in one transaction; phase 1 and v1 layouts still read and migrated |
| R25 | N/A | nothing installed into user-owned locations |
| R26–R30 | Complies | schema re-checked per connection; hashed bytes are the bytes sent; per-item compare-and-set; nothing deleted that is not pulsar's; the claim is committed before the send |
| R31 | Deviation | [a stale delete is re-armed](./publishing/4_decisions.md#a-stale-delete-is-re-armed-and-a-lost-delete-is-retryable) |
| R32 | N/A | no Git automation |

## STD-04@1 — Testing and verification

| Rule | Status | Evidence |
|---|---|---|
| R1 | Complies | tests drive the CLI, the MCP session and `orbit_tool.handle` |
| R2, R3, R4 | Complies | [AGENTS.md](../../AGENTS.md) "Verification and handoff"; mutation runs recorded in [ORB-13138] |
| R5 | Complies | behaviour assertions with messages |
| R6 | Complies | tests run serially; `hermetic_env` resets environment and the live-credential set per test |
| R7 | Complies | fake transport; no network (`tests/conftest.py::FakeX`) |
| R8 | Complies | `-rs` reports skips; the launcher test never skips |
| R9, R10 | Complies | no retries or sleeps as fixes; pytest exits non-zero when it collects nothing |
| R11 | Complies | goldens regenerated in the same change (`PULSAR_UPDATE_GOLDENS=1`) |
| R12 | Complies | `make check` runs the standards check, lock check, ruff, basedpyright and pytest |
| R13, R14 | Complies | `tests/test_docs.py` parses documented commands, links and frontmatter |
| R15–R22 | Complies | [AGENTS.md](../../AGENTS.md) "Verification and handoff" |

## STD-05@1 — Security boundaries

| Rule | Status | Evidence |
|---|---|---|
| R1, R2, R3 | Complies | identity from the stored binding checked against X; the caller label is audit only; every write surface enforces policy in core |
| R4 | N/A | no authority-raising override |
| R5 | Complies | the store says Fernet is no boundary between same-uid processes |
| R6, R7 | Complies | media confined by resolved roots and `O_NOFOLLOW` walks (`core/media.py`) |
| R8, R9 | Complies | explicit modes; `require_private` on the home and every secret file; launcher `umask 077` |
| R10, R11 | N/A | no untrusted children; environment variables grant nothing |
| R12 | Complies | tokens never in argv or environment |
| R13 | Complies | `REDACTED_COLUMNS` inventory; logs through `RedactingFilter` |
| R14 | Complies | redaction matches loaded token and key values and credential shapes, never vocabulary (`core/guard.py`) |
| R15 | N/A | pulsar holds its own grant; it copies no operator credentials |
| R16 | Complies | exact loopback Host allow-list (`surfaces/mcp.py`, `providers/x/auth.py`) |
| R17 | Deviation | [a POST without an Origin is an MCP client](./surfaces/4_decisions.md#a-post-without-an-origin-is-an-mcp-client-not-a-browser) |
| R18 | N/A | no non-HTTP listener |
| R19, R20 | Complies | loopback binds only; network only on operator-invoked calls |
| R21, R22 | N/A | no consent store yet; no registry of untrusted names |
| R23, R24, R25 | Complies | hashed `uv.lock`, `uv sync --frozen`; dependabot; `secrets` and `os.urandom` |

## Task References

- [ORB-13138] — aligned pulsar with the adopted standards and wrote this record.
- [ORB-13207], [ORB-13208], [ORB-13210]–[ORB-13216] — the open gaps above.

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.
