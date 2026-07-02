# Ralph Memory Prompt (v1)

Ralph keeps cross-iteration memory in exactly **two tiers**, split by durability
(ADR-0005). There is **no** progress.txt — do not create one, and do not read one.
Honor `CONTEXT.md` terminology: this is a **HIL** (human-in-the-loop) loop.

## The two tiers

- **Learnings** — durable, reusable knowledge (conventions, gotchas, HAL patterns)
  that outlives the story. These live in nested `AGENTS.md` files committed in the
  superproject.
- **Story notes** — transient, story-specific state. These stay on the **issue**
  (the Handoff comment / issue thread), never in `AGENTS.md`.

## At the start of a story

Read the nearby nested `AGENTS.md` files, **nearest-first**, from the directory you
are working in up to the repo root (`ralph --read-learnings <dir>`). Read only the
relevant local files, not one growing global brain-dump — that is the point of
nesting them per module and keeping each **lean**.

## Before completing a story

When you discover a genuinely reusable convention, gotcha, or HAL pattern, **promote**
it to the **nearest** `AGENTS.md` for the code you touched (`ralph --learn-target
<changed-file>`). Keep the entry **module-local**: prefer the closest existing
`AGENTS.md`, and when none exists, create a lean one in that module's own directory
rather than dumping everything at the root.

Do **not** promote story-specific details (what you did this iteration, the current
diff, resume state) — those belong on the issue as story notes, not in `AGENTS.md`.
Keep each `AGENTS.md` short and local; if one is growing into a general log, split it.
