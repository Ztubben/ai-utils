---
name: ralph-story
description: Break a plan into canonical Ralph backlog stories — GitHub issues carrying state:/type:/prio: labels, a Depends on: line, an Acceptance Criteria checklist, and (for HIL) a Bench Test Procedure. Specializes to-issues for the label-driven Ralph Loop. Use when authoring or formatting stories the Ralph Loop selection engine will read.
---

# Ralph Story Formatting

Specialize [`to-issues`](../to-issues/SKILL.md) so the issues it emits are in the **canonical
Ralph backlog shape** — the labels + issue-body encoding that the Ralph Loop reads as its
machine-readable source of truth (ADR-0002). Everything in `to-issues` still applies
(tracer-bullet vertical slices, quiz the user, publish blockers first); this skill pins the
label vocabulary and body template, and adds AFK/HIL classification, Blocker handling, an
advisory sizing checklist, and HIL bench procedures.

Terminology is standardized on **HIL** (Human-In-the-Loop), never HITL. Use the glossary in
`CONTEXT.md`.

## Canonical shape

Every story is a GitHub issue. The machine-readable state lives in **labels**; the rest lives
in the **body**. Do not invent new label namespaces — the `state:`/`type:`/`prio:` scheme is
mandated and not overridable (it is fixed in the config schema, ADR-0002).

### Labels

- **State** (exactly one, mutually exclusive):
  `state:ready` → `state:in-progress` → `state:awaiting-bench` → closed (= Passing/Done).
  New stories authored for the loop start at `state:ready`.
- **Type** (exactly one): `type:afk` or `type:hil`.
  - `type:afk` — acceptance criteria are fully verifiable by CI alone (pure software, parsing,
    refactors, build config). Passing as soon as CI is green.
  - `type:hil` — acceptance requires human verification on the physical bench (GPIO, timing,
    sensors, actuators). Passing only when CI is green **and** a human bench-verifies it.
- **Priority** (optional, at most one): `prio:N`, lower = higher priority. A story with no
  `prio:N` sorts as lowest priority; ties (and prio-less stories) break by lowest issue
  number (FIFO). Priority is explicit labels, not issue-number ordering, so stories can be
  reprioritized without renumbering — add `prio:N` only to jump the queue.

### Body

Use the template below. A story that needs a **human design decision before any code** is not a
third type — it is a **Blocker**: keep it out of `state:ready` (label it `ready-for-human`) so
the selection engine never picks it up until a human resolves the question and reclassifies it
as `type:afk`/`type:hil`.

## Classify every story AFK or HIL

Ask: *can CI alone prove this Passing, with no hardware in the loop?*

- Yes → `type:afk`. Prefer AFK wherever possible.
- No, a human must confirm real behavior on the bench → `type:hil`. The host-testable logic
  (decision logic against a fake HAL) is still covered by acceptance criteria; only the
  genuinely hardware-coupled behavior is deferred to the `## Bench Test Procedure`.
- Neither yet, because a design decision is missing → **Blocker** (`ready-for-human`, not
  `state:ready`), not a type.

## Advisory sizing checklist (no hard gate)

Each story must fit a single agent context window and be independently verifiable. This is
**advisory** — propose splits for the human to approve; do not compute token estimates and do
not block on it. Flag a story for a possible split when it:

- touches many modules or crosses several integration layers at once,
- has more than ~5–7 acceptance criteria,
- mixes AFK and HIL work (split the pure-logic slice from the hardware-coupled slice),
- bundles independent behaviors that could each be demoed on their own.

Present proposed splits in the `to-issues` quiz step and let the human decide.

## Issue body template

```markdown
## What to build

A concise description of this vertical slice — the end-to-end behavior, not layer-by-layer
implementation. Avoid file paths and code snippets; they go stale.

## Acceptance Criteria

- [ ] Criterion 1 (observable, CI-checkable for AFK)
- [ ] Criterion 2
- [ ] Tests pass

## Bench Test Procedure   ← HIL stories only

1. Numbered steps a human runs on the physical bench to verify the hardware-coupled behavior.

Parent: #41              ← PRD issue number for Feature stories, or `Parent: None` for Orphan Stories
Depends on: #12, #34    ← or `Depends on: None`
```

Rules the template encodes (verify with `ralph --lint-story`, below):

- Exactly one `state:` and one `type:` label (except Blockers); at most one (optional) `prio:` label.
- A `## Acceptance Criteria` heading with at least one `- [ ]` checklist item.
- HIL stories additionally carry a `## Bench Test Procedure` section.
- A `Parent:` line linking the story to its Feature's PRD issue (`Parent: #N`), or
  `Parent: None` for an Orphan Story (one that belongs to no Feature). The `Parent:` line is
  required on every story (ADR-0002).
- A `Depends on:` line (either `None` or `#`-prefixed issue numbers). A story is ineligible
  until every dependency is Passing; an AFK dependency counts as satisfied only when merged, a
  HIL dependency only when bench-verified (closed).
- **cross-Feature dependency prohibition**: a Feature story's `Depends on:` must reference only
  same-Feature stories, Orphan Stories, or PRDs — never a story from another Feature (its code
  lives on a different branch and is unreachable). Cross-Feature ordering is expressed as a
  `Depends on:` line between PRD issues; every Feature story inherits its PRD's unsatisfied
  dependencies (ADR-0006).
- HIL, never HITL.

## Verify the output

Well-formed examples the selection engine can consume ship under
[`examples/`](./examples/): an AFK story, a HIL story (with a bench procedure), and a Blocker.

Lint any rendered story (the `gh issue view --json number,title,labels,body` shape) before
publishing:

```sh
ralph --lint-story path/to/story.json     # or: gh issue view N --json ... | ralph --lint-story -
```

It exits 0 and prints a one-line summary for a canonical story, or non-zero and names the
offending field(s) otherwise. Use it to confirm example runs produce issues the loop can read.
