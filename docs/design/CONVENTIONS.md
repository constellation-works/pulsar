---
title: Design Doc Conventions
owner: claude
last_updated: 2026-09-26
last_validated: 2026-09-26
status: Accepted
---

# Design Doc Conventions

How pulsar's design docs under `docs/design/<feature>/` are laid out and kept. They follow
Orbit's conventions (orbit repository, `docs/design/CONVENTIONS.md`) so the two read as one
documentation system; this file records the pulsar specifics and is the source of truth for
this repository. When a convention changes, update this doc first, then the folders.

The README is the front door: what pulsar is, how to install, authorize and run it. Anything
longer than that lives here.

---

## 1. Folder Layout (per feature)

```
docs/design/<feature>/
├── 1_overview.md       what and why
├── 2_design.md         current implementation
├── 3_vision.md         forward-looking: open questions, prior work
├── 4_decisions.md      titled decisions and their reasoning
├── specs/              prescriptive contracts, one mechanism per file
└── references/         lookup material: glossary, tables
```

- Folder name: lowercase, hyphenated, singular (`accounts`, `publishing`).
- No `README.md`, `roadmap.md`, `changelog.md` or `tutorial.md` in a feature folder. The
  roadmap is Orbit tasks; the changelog is git history; the tutorial is the top-level README.
- `specs/` and `references/` are optional; create them when there is something to put in.

Start a feature from the scaffold:

```sh
cp -r docs/design/_templates docs/design/<feature>
mv docs/design/<feature>/specs/_mechanism.md docs/design/<feature>/specs/<mechanism>.md
```

---

## 2. Frontmatter

Every numbered doc starts with the frontmatter in [`_templates/`](./_templates/):

- `title` mirrors the H1.
- `owner` is the accountable agent family (`claude`, `codex`, `gemini`, `grok`).
- `last_updated` is the date of the last meaningful content change; `last_validated` the last
  time someone checked the doc against the code.
- `status` is `Draft` until the owner has reviewed it against the code, then `Accepted`.
- `feature` is the folder slug; `doc_role` is `overview`, `design`, `vision` or `decisions`.
- `type`, `summary` (one non-empty line), `tags`, `paths` (globs under `src/`), `related_features`
  and `related_artifacts` (task ids) make the doc retrievable by search.

---

## 3. Required Sections

| File | Sections, in order |
|------|--------------------|
| **1_overview.md** | Elevator paragraph · §1 Motivation · §2 Core Concepts · §3 At a Glance (concern → file → task) · Task References |
| **2_design.md** | Scope paragraph · numbered mechanism sections · Concerns & Honest Limitations (last, mandatory) · Task References |
| **3_vision.md** | Scope paragraph · §1 Open Questions · §2 Prior Work · §3 What May Be Distinctive · §4 References · Task References |
| **4_decisions.md** | Scope line · titled entries: Recorded · Context · Decision · Consequences (with `Cost:`) · Task References |

Every numbered doc ends with **Task References**: the task ids it cites, each with a verb
phrase, and the line

> Resolve any task above with `orbit task show <ID>` or `git log --grep=<ID>`.

---

## 4. Decisions

A decision is a titled section in its feature's `4_decisions.md`, changed in the same commit as
the code it explains. It is admitted through one of two doors; most choices go through
neither and belong in `2_design.md` prose.

- **Door 1: it explains surprising code.** A reader will hit a specific site, think it is
  wrong, and be right until they know the decision. The entry carries `**Code anchors:**`
  (`path::symbol`) or `**Paths:**`.
- **Door 2: it governs future decisions.** A standing rule written so it applies to a case
  nobody has seen yet ("a missed post beats a double post"). A retrospective account of one past
  choice is not Door 2.

Either way the entry names a real alternative and a non-trivial `Cost:`. Format:

```markdown
## <specific, unique title>

**Recorded:** YYYY-MM-DD · [ORB-NNNNN]
**Code anchors:** `src/pulsar/<path>.py::<symbol>`

### Context
### Decision
### Consequences
- Cost: <what this gives up>
```

- The title is the address: link it as `[Title](./4_decisions.md#title-anchor)`.
- Supersede with `**Superseded by:** [New title](#new-title)`; the old entry stays.
- Decisions made in Orbit (the plugin standard, sandbox, envelope) are Orbit's; cite them in
  prose by repository and title, and record here only what pulsar chose in response.

---

## 5. Specs and References

- A **spec** (`specs/<mechanism>.md`) is prescriptive: a one-paragraph contract, **Why This
  Exists**, then invariants, failure modes and migration paths. Rationale goes in
  `4_decisions.md`. Specs are what tests should be checkable against.
- A **glossary** is an intro paragraph plus an alphabetized `Term | Meaning` table of
  pulsar-specific vocabulary, each entry pointing at the doc that uses it.
- Other lookup tables (error codes, tool tables, config keys) go in `references/` of the
  feature that owns them.

---

## 6. Links and Task IDs

- Relative links only, with a `./` or `../` prefix. Code links go from the doc to the file
  (`../../../src/pulsar/app/core/publishing/publisher.py`).
- Task ids are plain bracketed text, never links: `[ORB-13028]`. pulsar's tasks live in
  Orbit workspace `ws_pulsar` on the posting host; Orbit-side gaps live in `ws_orbit`. Both use
  `ORB-` ids.
- Never cite a task without saying what it did.

---

## 7. Keeping Docs True

- A behaviour change updates its design doc (and the README, if the front door changed) in the
  same commit.
- `make check` checks what a machine can: relative links resolve, design docs carry their
  frontmatter, and every documented `pulsar` command parses (`tests/test_docs.py`). The rest
  is review: a reviewer treats this file as a checklist, the author decides whether a
  deviation is justified.
- A retired feature's folder is deleted in the retiring change; git keeps the history.

---

## 8. Ownership

| Feature | Folder | Lead |
|---------|--------|------|
| Accounts | [accounts/](./accounts/) | claude |
| Publishing | [publishing/](./publishing/) | claude |
| Channels | [channels/](./channels/) | claude |
| Surfaces | [surfaces/](./surfaces/) | claude |
| Engagement | [engagement/](./engagement/) | claude |

The lead keeps the folder in sync with the code and answers review comments. Anyone may edit.
