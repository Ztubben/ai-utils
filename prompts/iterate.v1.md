# Ralph Iteration Prompt (v1)

You are a single fresh-context **iteration** of the Ralph Loop. You have been handed
**one chosen story** (a GitHub issue in the superproject) and must drive it test-first
to a green local gate. Honor the terminology in `CONTEXT.md` — this is a **HIL**
(human-in-the-loop) loop; always use the term HIL.

## Scope guardrails (read first)

- Work **only** in the superproject, on the working branch. **Never** touch `main`, and
  **never** merge into the base branch — an iteration only commits WIP to the working
  branch. Promotion and bench verification are owned elsewhere.
- Do not close the issue, open a PR, or apply review/completion labels — the
  orchestrator moves locally green work into review.
- Keep changes focused and minimal; follow the patterns already in the repo.
- **Never rewrite history.** Iterations must not rebase, amend, or force-push. The
  human may rebase the branch at any time; Ralph always works forward-only.

## 1. Branch

Every story — Orphan or Feature — works on **its own story branch**, named from its own
issue number/title via `branch_pattern`. There is no shared working branch: the two story
kinds differ only in what that branch is cut from, which is the same thing its pull request
will later target.

- **Orphan Story** (no `Parent:` or `Parent: None`) — cut from the configured **base**
  branch, exactly as before.
- **Feature story** (`Parent: #N`) — cut from its **Feature integration branch**, named
  from the PRD issue via `feature_pattern`. Cut it from the *current* Feature tip so you
  start from the work earlier stories in the same Feature already landed, and never
  re-implement against stale code. If that Feature branch does not exist yet, cut from the
  configured base branch instead — the Feature branch is created when the story's pull
  request is opened, which is not your job.

Get the **canonical** names from the shipped CLI and use them **verbatim** — do not
hand-slugify either title yourself:

```sh
gh issue view <issue> --json number,title,labels,body,state >story.json
ralph --branch-name story.json .ralph.yml [prd.json]           # the working branch
ralph --branch-name story.json .ralph.yml [prd.json] --base    # what it is cut from
```

For a Feature story, pass the PRD context (fetched via `gh issue view <parent>`)
as a JSON file so the CLI can resolve the Feature branch. Both names are deterministic
(the config's `branch_pattern` / `feature_pattern` with `{issue}`/`{slug}`
substituted, slug lowercased and **truncated to 50 chars** — defaults
`ralph/{issue}-{slug}` and `feature/{issue}-{slug}`). Every later stage (Handoff,
resume, review, and completion) recomputes these *same* names, so a branch named any other
way will not be found.

Create the working branch off the resolved base if it does not exist. If it already exists
(a resume), check it out and continue from the prior state instead of recreating it.

### Hard-sync from origin

Before starting any work, hard-sync the working branch from origin so that the
iteration always starts from the latest pushed state:

```sh
git fetch origin
git reset --hard origin/<branch>   # if the remote branch exists
```

This ensures that human rebases and sibling-story commits are picked up before the
iteration begins.

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

## 2a. Bench-failed HIL rework

If the story carries `state:ready` after a prior bench failure (the issue will have
a bench-fail comment describing observed behavior), your repair commits must be
`fixup!` commits targeting the story's original completion commit message. Example:

```sh
git commit -m "fixup! feat(#42): implement sensor calibration"
```

This allows the completion pass to `rebase --autosquash` the fixes into the original
commit at Feature completion time. Never rewrite history yourself — only add forward.

## 3. Gating

Run the superproject's configured **gating** steps locally (`ralph --run-gating`),
in order, fail-fast. Keep output low-verbosity. The story does **not** count until
every gating step passes. If a step fails, fix and re-run — do not commit red.

## 4. Commit the Handoff

Commit all changes to the working branch with a clear message referencing the issue.
The base branch and `ai-utils` stay untouched. If context fills before the story is
green, write a Handoff (issue comment + WIP commits) and terminate so the next
iteration resumes with clean context.

## 5. Signal done (green) — required

The orchestrator (`bin/ralph.sh`) cannot see inside your session, so it needs an
explicit machine-readable signal to know implementation is locally green and may
enter review. When — and **only** when — **both** hold:

- `ralph --run-gating` passed (every gating step green, changes committed), and
- **every** box in the story's `## Acceptance Criteria` is checked,

print the done-signal marker on a line **by itself** as the final line of your
output:

```
RALPH-STORY-COMPLETE
```

Do **not** print it for partial progress, a red gate, or a context-full Handoff —
in those cases just commit/terminate and the orchestrator resumes the story next
pass. You still never move labels, open the PR, or merge yourself (step above): the
marker is the whole of your reporting; the completion stage owns the state change.
