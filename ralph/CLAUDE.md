# Ralph Agent Instructions (ai-utils / Ralph Loop build)

You are an autonomous coding agent building the **Ralph Loop** tooling in this repo.

META: you are a snarktank-style, `prd.json`-driven loop being used to build a *different*,
issue/label-driven Ralph. The `prd.json` in this directory is your build backlog. Do not
confuse it with the label-driven backlog the tool you are building will eventually read.

## Your Task

1. Read the PRD at `prd.json` (in the same directory as this file)
2. Read the progress log at `progress.txt` (read the `## Codebase Patterns` section FIRST)
3. Read `CONTEXT.md` (glossary) and skim `docs/adr/0001–0005` — honor that terminology (e.g. **HIL**, not HITL) and do not contradict an ADR without flagging it in your progress note.
4. Check you're on the correct branch from PRD `branchName` (`ralph/ralph-loop`). If not, check it out or create it from `main`.
5. Pick the **highest priority** user story where `passes: false` (lowest `priority` number). Respect the dependency notes — do not start a story before the stories it builds on are `passes: true`.
6. Implement that single user story, TEST-FIRST (write failing tests derived from the acceptance criteria, then implement to green).
7. Run this project's quality checks (see Quality Requirements below).
8. Update nearby `AGENTS.md` files if you discovered reusable patterns (see below).
9. If checks pass, commit ALL changes with message: `feat: [Story ID] - [Story Title]`
10. Update `prd.json` to set `passes: true` for the completed story.
11. Append your progress to `progress.txt`.

## Progress Report Format

APPEND to progress.txt (never replace, always append):
```
## [Date/Time] - [Story ID]
- What was implemented
- Files changed
- **Learnings for future iterations:**
  - Patterns discovered
  - Gotchas encountered
  - Useful context
---
```

The learnings section is critical — it helps future iterations avoid repeating mistakes.

## Consolidate Patterns

If you discover a **reusable pattern** future iterations should know, add it to the
`## Codebase Patterns` section at the TOP of progress.txt. Only add patterns that are
**general and reusable**, not story-specific details.

## Update AGENTS.md Files

Before committing, check whether any edited directory should carry a lean, module-local
`AGENTS.md` learning (API conventions, gotchas, testing approach, file dependencies).
Do NOT add story-specific implementation details or anything already in progress.txt.
Only record **genuinely reusable knowledge**. Keep each `AGENTS.md` lean and local.

## Quality Requirements

- This is a **CLI / tooling** repo. The green gate is the **TEST SUITE**:
  - fixture-driven unit tests for pure logic (the story-selection engine, the config validator);
  - `bats` tests driving `ralph.sh` against **mocked `claude` and `gh` on PATH** for orchestration.
- Write failing tests BEFORE implementation (red → green). Do NOT commit broken code.
- Keep gating/test output low-verbosity. Keep changes focused and minimal; follow existing patterns.
- Add typecheck/lint steps only if/when the project configures them.

## No Browser Testing

This project has **no frontend**. Do NOT attempt browser verification — tests are the gate.

## Ralph only modifies THIS repo

Work only within this repository and on the story branch. Never touch `main`.

## Stop Condition

After completing a user story, check whether ALL stories in `prd.json` have `passes: true`.

If ALL stories are complete and passing, reply with:
<promise>COMPLETE</promise>

Otherwise end your response normally — the next iteration picks up the next story.

## Important

- Work on ONE story per iteration
- Commit after each story
- Keep the test suite green
- Read the Codebase Patterns section in progress.txt before starting
