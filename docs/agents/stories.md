# Stories, selection and topology — module notes

Notes for `lib/ralph_story.py`, `lib/ralph_select.py`, `lib/ralph_iterate.py`, and the Story-is-the-unit-of-the-pull-request topology (PRD #69). Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

- A "story" is a GitHub issue in `gh issue view --json number,title,labels,body` shape
  (labels as `{"name": ...}` objects); `lib/ralph_story.py` normalizes labels (accepts
  objects or plain strings) and is the canonical story-format checker the selection engine
  builds on. Fixtures for story-shaped logic live under `test/fixtures/stories/`.
- `lib/ralph_select.py` is the pure selection engine (`normalize` → `select_next` →
  `Action`). It reuses `ralph_story`'s field extraction but owns ordering (optional prio
  ascending — absent prio sorts last — ties by lowest issue number, FIFO) and dependency
  satisfaction. The scan must request the
  gh `state` field. Active Stories (`state:in-progress` or `state:in-review`) resume
  before any Ready Story starts. A `Depends on:` edge is satisfied only when the
  referenced issue is
  closed (an AFK dep once merged, a HIL dep once bench-verified — both surface as closed).
  Don't confuse gh's `state` (OPEN/CLOSED) with the `state:` label (ready/in-progress/…).
  Backlog fixtures (JSON arrays of gh-shaped issues) live under `test/fixtures/backlogs/`.
- `lib/ralph_iterate.py` holds the deterministic seams of one iteration: `branch_name`/
  `slugify` (pure — story branch from `branch_pattern`, `{issue}`/`{slug}` substituted),
  `resolve_topology` (#70, PRD #69 — the two names one Story has: the working branch it
  commits on and the base its pull request targets; `resolve_branch` is the first half.
  Every Story works on its own story branch, Orphan or Feature; **only the base differs**,
  an Orphan Story's being `branching.base` and a Feature Story's its Feature branch from
  `feature_pattern` over the PRD issue. Resolved together on purpose — a caller that
  computed them apart could push one Story's branch and open its pull request against
  another Story's base. `--branch-name` still prints one branch name by default, so
  existing callers are untouched, and reports the base under `--base`) and
  `run_gating` (shells the configured steps in order, fail-fast, captures stdout+stderr,
  returns a `GatingResult`). `--run-gating` is low-verbosity: passing steps print only a
  check line, a failing step's output goes to stderr. The judgment-heavy TDD itself lives
  in the checked-in **agent prompt** `prompts/iterate.v1.md`; a unit test drift-guards its
  required directives (red/green, off-target HAL, gating, `{issue}`/`{slug}`, never touch
  base/main, HIL not HITL). Gating-config fixtures live under `test/fixtures/gating/`.
- **The Story is the unit of the pull request** (PRD #69, #70–#77). Read ADR-0006's
  *Amended* bullets before touching branching, completion, or round counting: a Feature's
  Stories no longer share a working branch or a pull request, so the Negotiation Round
  budget is counted on the **Story** (`ralph_review.rounds_spent(comments)`, off the
  recorded review results) and never on the pull request (#72) — the old count was the
  *Feature's*, and a live deployment escalated a Feature's third Story at `max_rounds` with
  zero rounds against it. The physical gate moved with the topology: `ralph_feature.
  unverified_hil_stories` refuses a Feature integration while any of its HIL Stories is
  open, naming each, and the live CLI **fails closed** on a backlog it cannot read (#74).
  `branching.rescue_pattern` and the feature-branch boundary record are gone (#75).
