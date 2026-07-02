# Ralph Iteration Prompt (v1)

You are a single fresh-context **iteration** of the Ralph Loop. You have been handed
**one chosen story** (a GitHub issue in the superproject) and must drive it test-first
to a green local gate. Honor the terminology in `CONTEXT.md` — this is a **HIL**
(human-in-the-loop) loop; always use the term HIL.

## Scope guardrails (read first)

- Work **only** in the superproject, on the story branch. **Never** touch `main`, and
  **never** merge into the base branch — an iteration only commits WIP to the story
  branch. Promotion and bench verification are owned elsewhere.
- Do not close the issue, open a PR, or apply completion labels — later stages
  (AFK auto-merge / HIL awaiting-bench) do that.
- Keep changes focused and minimal; follow the patterns already in the repo.

## 1. Branch

Create the story branch off the configured **base** branch using the config's
`branch_pattern` (`{issue}` and `{slug}` are substituted — default
`ralph/{issue}-{slug}`). If the branch already exists (a resume), check it out and
continue from the prior Handoff instead of recreating it.

## 2. Red → Green (test-first)

1. Read the story's `## Acceptance Criteria` checklist. Each unchecked box is a
   behavior you must make observable.
2. Write **failing** tests derived from those acceptance criteria **before** writing
   any implementation. Confirm they fail (**red**) for the right reason.
3. Implement the smallest change that turns them **green**. Refactor only once green.
4. Test the logic **off-target** on the host: never require real hardware in a unit
   test. Exercise device-coupled code against a **fake/mock HAL** so the whole slice is
   verifiable in CI. Only genuinely hardware-coupled behavior is deferred to a HIL
   story's `## Bench Test Procedure`.
5. Every story is independently verifiable: the code you add must be reached and
   exercised by a test, never orphaned.

## 3. Gating

Run the superproject's configured **gating** steps locally (`ralph --run-gating`),
in order, fail-fast. Keep output low-verbosity. The story does **not** count until
every gating step passes. If a step fails, fix and re-run — do not commit red.

## 4. Commit the Handoff

Commit all changes to the story branch with a clear message referencing the issue.
The base branch and `ai-utils` stay untouched. If context fills before the story is
green, write a Handoff (issue comment + WIP commits) and terminate so the next
iteration resumes with clean context.
