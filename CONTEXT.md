# ai-utils

Reusable, project-agnostic tooling shared across embedded projects as a git submodule. Hosts the Ralph Loop machinery and supporting skills. Contains no project-specific source, issues, or CI.

## Language

**Superproject**:
The parent embedded project that mounts `ai-utils` as a submodule. The Ralph Loop runs against the superproject and only ever modifies the superproject — never `ai-utils` itself.
_Avoid_: parent repo, host project (use "superproject")

**Ralph Loop**:
The autonomous coding agent loop that scans the superproject's backlog for workable stories and implements them test-first. Shipped from `ai-utils`, executed from the superproject root. Ralph integrates into the configured base branch (default `develop`) and **never touches `main`**; promoting `develop → main`, including any slower-cadence integration/regression bench testing, is human-owned and outside Ralph's scope.
_Avoid_: the agent, the bot, the runner (use "Ralph Loop" or "Ralph")

**Story**:
A single unit of backlog work, tracked as a GitHub Issue on the superproject. Every story is classified as either an AFK Story or a HIL Story.
_Avoid_: task, ticket, card (use "story")

**AFK Story** (Away-From-Keyboard):
A story whose acceptance criteria are fully verifiable by CI alone, with no hardware in the loop (e.g. pure-software logic, parsing, refactors, build config). It is Passing as soon as CI is green; Ralph may immediately continue to the next story.

**HIL Story** (Human-In-the-Loop):
A story whose acceptance criteria require human verification on the physical bench (e.g. GPIO, timing, sensors, actuators — anything whose real behavior CI cannot observe). After Ralph implements it and CI is green, it enters Awaiting Bench Verification and is not Passing until the human confirms it on the bench. There are exactly two runtime story types (AFK, HIL); a story needing a human *design decision before coding* is not a third type — it is a Blocker (kept out of `state:ready`, e.g. `ready-for-human`) until the human resolves it.
_Avoid_: HITL (use "HIL")

**Bench-testable**:
A property of a HIL story: its acceptance criteria can be verified by a human on the physical hardware bench, one story in isolation, without depending on other unverified work.

**Independently verifiable**:
A required property of every story: the code it adds must be reachable and exercised, never orphaned. Implementing a function or class that nothing calls is disallowed, because unexercised code cannot be verified (by CI for AFK stories, or on the bench for HIL stories). Each story is a vertical slice that produces observable behavior.
_Avoid_: vertical slice, tracer bullet (use "independently verifiable")

**Gating steps**:
The project-specific set of quality checks Ralph must run and pass before a story counts (e.g. build, unit tests, lint). Declared by each superproject, run **locally** by Ralph (a local mirror of CI, kept low-verbosity to save cost and context), and configurable — the superproject decides which steps Ralph runs.

**Awaiting Bench Verification**:
The state a HIL story enters after Ralph has implemented it and CI is green, but before the human has confirmed the behavior on the bench. Ralph may keep implementing other stories that are **not blocked by** a story in this state; it must not start a story that depends on one.

**Passing** (a.k.a. **Done**):
For an AFK story: CI is green. For a HIL story: CI is green **and** the human has bench-verified it. Ralph making CI green is never sufficient to mark a HIL story Passing.
_Avoid_: complete, finished, closed (use "passing" / "Done")

**Tick**:
One scheduled run of the Ralph Loop, triggered every 5 hours by the local scheduler and bounded by the Claude session window. A tick first resumes any story already in `state:in-progress` (from a prior checkpoint) before scanning for new `state:ready` work, then works as many eligible stories as the session budget allows. Only one tick per superproject runs at a time, guarded by a `flock` lockfile in `.git/`; an overlapping tick exits immediately. When the session limit is reached the current iteration checkpoints (Handoff) and the tick ends cleanly.
_Avoid_: run, cron run (use "tick")

**Iteration**:
A single fresh-context agent process within a tick. Ralph **never compacts context**: when context fills, the current iteration terminates after writing a Handoff, and the next iteration resumes the same story with clean context. Stories must be sized to fit within a single context window.
_Avoid_: pass, loop, session (use "iteration")

**Handoff**:
The summary an iteration leaves for the next so a story can resume with clean context: what was done, and the code implemented so far. Stored in the superproject only — the summary as an issue comment (story in `state:in-progress`), the code as WIP commits on the story branch (`ralph/<issue#>-slug`), never on `main`. A context-full termination that produces a Handoff is a normal checkpoint, not a failed Attempt.

**Learnings**:
Durable, reusable knowledge (conventions, gotchas, HAL patterns) Ralph records in nested `AGENTS.md` files in the superproject, read at story start and updated on genuine discoveries. Global across all features. Distinct from a Handoff, which is transient per-story resume state. At story completion Ralph promotes reusable knowledge to `AGENTS.md` and leaves story-specific notes on the issue. There is no `progress.txt`.
_Avoid_: progress log, patterns file (use "learnings" / "AGENTS.md")

**needs-human**:
The label Ralph applies (with a comment tagging the user) when the circuit breaker trips — a second story has failed to `state:blocked` — signalling the loop has halted and the user must intervene and reset.

**Attempt**:
An iteration that ends **without the story reaching green** (gating steps failed, or Ralph is stuck). A context-full checkpoint is not an Attempt. After a configurable number of failed Attempts (default 3), the story moves to `state:blocked`.

**Blocker**:
A condition that makes a story ineligible for Ralph to pick up: an open dependency on a story that is not yet Passing (for a HIL dependency that means bench-verified, not merely CI-green), a missing design decision, or an explicit block label. Ralph only works Ready stories with no open blockers.
