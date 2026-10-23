---
name: ralph-backlog-audit
description: Audit a Ralph Loop backlog (GitHub issues with state:/type:/prio: labels) for drift — stories already implemented but still open, resolved or impossible Blockers and Depends on: edges, lint failures against the canonical ralph-story shape, PRDs whose stories are all done, stale issues, and stories that no longer match the code, ADRs or CONTEXT.md. Produces a report of proposed fixes and applies only the ones the user approves. Use whenever the user asks to groom, clean up, review, sanity-check or "go through" the backlog, asks why the Loop picks the wrong story or finds no work, asks what is blocked or unblocked, or wants to know whether issues are still up to date — even if they never say "audit".
---

# Ralph Backlog Audit

The Ralph Loop trusts the backlog completely: labels and `Depends on:` lines are its only
source of truth (ADR-0002). When work lands by hand, a dependency is closed as not planned,
or an issue is filed without the canonical shape, the Loop quietly does the wrong thing. It
re-selects finished stories, waits forever on an unreachable dependency, or reports
`no-work` while unlabeled stories pile up. This skill finds that drift and repairs it with the
user's approval.

[`ralph-story`](../ralph-story/SKILL.md) is the authority on what a well-formed story looks
like. This skill audits against it and repairs toward it, so read its *Canonical shape* and
*Issue body template* before proposing body or label fixes. Use the glossary in `CONTEXT.md`
(HIL, never HITL; Blocker; Passing; Orphan Story; Feature).

## Ground rules

- **Read-only until approved.** Gather evidence, then present the report, then edit. Issue
  edits are visible to everyone and drive an unattended Loop, so the user decides.
- **Edit with `gh`, never with Ralph's state-mutating subcommands** (`--complete-*`,
  `--checkpoint`, `--record-attempt`, `--assign-models`, a tick). They perform Loop
  transitions with side effects (pushes, PRs, comments). A manual fix is a plain label or body
  edit. The read-only commands are fine: `ralph --lint-story`, `ralph --dry-run`.
- **Never mark HIL work Passing by inference.** A HIL story is Passing only once a human has
  verified it on the bench. Commits that look complete move it at most to
  `state:awaiting-bench`, and only with the user's say-so. Closing it would let dependents
  build on hardware behavior nobody has confirmed.
- **Evidence is a lead, not a verdict.** A commit whose subject names `#N` may be a red test,
  a partial slice, or a reference to another repository's issue. Check what it actually did.

## Workflow

### 1. Snapshot

Run the bundled script from the repository whose backlog you're auditing:

```sh
python3 <this-skill>/scripts/backlog_snapshot.py --json /tmp/…/snapshot.json
```

It lists every open issue with the Loop's own lint result and dependency verdicts. It
imports `lib/ralph_story.py` and `lib/ralph_select.py`, so "satisfied" means exactly what the
selection engine decides. It also records commits whose subject names the issue, PRs that
close or mention it, age, and what the Loop would do next. It finds `lib/` relative to
itself or via `$RALPH_HOME`. It never writes to GitHub.

Each flag points at a check below. Unflagged issues still get a quick read in step 2 for
drift, because the script can't judge meaning.

### 2. Verify each finding

Work through the categories in this order. Earlier fixes change later answers: closing a
finished story can resolve half the dependency findings.

**Already implemented** (`maybe-implemented`, `state-drift`)
- Open the commit(s) (`git show --stat`, then the relevant hunks) and compare them with every
  Acceptance Criteria item. Note which items the code satisfies, and how you can tell (a test
  name, a function, a doc section).
- Confirm the commit is reachable from the base branch (`git branch -r --contains <sha>`, or
  `git merge-base --is-ancestor`). Work that sits only on a feature or story branch isn't
  Passing yet.
- Watch for red-first commits (`…, red (#N)`): a failing test alone means the fix is still
  pending, so look for the follow-up.
- Verdict: **close** (all AC met, AFK, on base), **partially done** (propose ticking the met
  boxes and say what remains), or **not done** (a false lead, so say nothing more).
- `in-review` with a merged PR is a finished story (close it, merge nothing). `in-review` with
  no PR at all needs a human look.

**Blockers and dependencies** (`waiting`, `dependency-suspect`, `blocker-resolved?`, cycles)
- `waiting` is normal queuing. It becomes a finding only when the dependency is itself
  finished-but-open (fix that one instead) or no longer needed.
- `missing` and `closed NOT_PLANNED` targets count as satisfied, so the dependent will run
  without the thing it said it needed. Ask whether the edge should be dropped, re-pointed at
  the story that replaced it, or whether the dependent is now obsolete too.
- `unreachable` means a dependency on another Feature's closed story, which the Loop can never
  satisfy. Propose a PRD-level `Depends on:` instead (ADR-0006, cross-Feature dependency
  prohibition in `ralph-story`).
- Cycles: propose which edge to cut and why.
- `ready-for-human` Blockers: read the comments. If the design question has been answered
  (a decision in a comment, an ADR, merged code), propose reclassifying the issue as
  `type:afk`/`type:hil` + `state:ready`, with any missing AC drafted from that decision.
- `state:blocked` stories: read the latest Ralph comment for the failure. Say whether it
  still applies, given later commits on base.
- Re-run the script with `--assume-closed <proposed closes>` to see what those closes
  unblock and what the Loop would select next. Put that preview in the report.

**Lint and shape** (`lint`)
- Draft a complete fix in the `ralph-story` template. Don't just paste the linter errors back.
  Infer missing fields from context and say where each came from: `Parent:` from the PRD
  the story was filed under or mentions; `type:` from whether CI alone can prove the AC; and
  `state:ready` only if it's actually workable. An incident report with no AC is a Blocker
  (`ready-for-human`) until someone decides what "fixed" means.
- Render the proposed issue as `{number,title,labels,body}` JSON and lint it before you
  propose it (`ralph --lint-story -`, or `bin/ralph` inside ai-utils). Only a proposal that
  passes goes in the report.
- A heading the linter can't recognize (`## Acceptance criteria`, `### AC`) makes the
  story look like it has no criteria. Fix the heading, and keep the checklist.
- An issue with no Ralph labels at all may not be meant for the Loop (a question, a note).
  Ask before converting it.

**Drift against code and docs**
- Read each open story's body for concrete references: functions, files, flags, labels,
  config keys, glossary terms. `grep` the code for them. Gone or renamed → propose an
  updated wording.
- Check the story against `CONTEXT.md` and `docs/adr/`. A story that assumes something an ADR
  since reversed (e.g. "main is refused as base") must be rewritten or closed as not
  planned.
- Look for overlaps: two open stories with the same AC, or one story's AC already covered by a
  closed sibling. Propose merge, split, or a `Related:` cross-link.
- `prd-stuck`: every child is closed, but the PRD lacks `state:ready`. The Loop's completion
  pass only integrates a Feature whose PRD carries it (breakdown finished), so the Feature
  never reaches the base branch. Check the PRD's goals aren't missing a story, then propose
  adding `state:ready`. Don't propose closing the PRD by hand: the completion pass merges the
  Feature and closes it. A closed PRD with open children is a finding too.
- Closed issues still carrying a `state:` label (listed at the end of the snapshot) do no
  harm to selection, but they mislead anyone filtering by label. Propose removing the label
  as a single low-priority batch.
- `stale`: an untouched issue isn't wrong by itself. Mention it only when it's also
  ambiguous, superseded, or low priority in a way the user should reconsider.

### 3. Report

Lead with the action-oriented summary, then the evidence. Use this structure:

```markdown
# Backlog audit — <repo> (<date>)

**Loop next action now:** <from snapshot>  →  **after proposed fixes:** <from --assume-closed>
<one-paragraph health summary: counts per category, the single most important problem>

## Proposed changes
### Close as done (N)
| # | Title | Evidence | AC verdict |
### Reclassify / relabel (N)
| # | Current | Proposed | Why |
### Rewrite body (N)
#N — <one line of what changes and why>  (full proposed body in the appendix, lints OK)
### Dependency edits (N)
| # | Current Depends on: | Proposed | Why |

## Needs your decision
Numbered questions the audit can't answer alone: obsolete or not, AFK or HIL, which edge to cut.

## No change needed
#… (one line, so the user can see everything was looked at)

## Appendix: proposed bodies
```

Give each proposed change an ID (`C1`, `R2`, …) so the user can approve by reference
("apply all but R2"). Keep evidence specific: a short SHA plus subject, or a test name,
beats "appears to be implemented".

If the backlog is long, it's fine to save the report as a markdown file in the repo's
scratch area and summarize it in chat. The tables are what the user acts on.

### 4. Apply approved changes

Apply only what the user approved:

- **Close as done (AFK):** remove the `state:*` label, post a short comment naming the
  evidence (commit SHAs, test names), then `gh issue close N --reason completed`. Close
  dependencies before dependents, so a running Loop never sees a dependent unblocked by an
  issue that's still open.
- **Close as obsolete:** comment why (what superseded it), then `gh issue close N --reason
  "not planned"`. Then check its dependents: they now see it as satisfied, which is usually
  wrong. Their edges should already be in the approved dependency edits.
- **Label changes:** `gh issue edit N --add-label … --remove-label …` in one call, so the
  issue never sits with zero or two `state:` labels.
- **Body rewrites:** write the approved body to a file, lint it once more, then
  `gh issue edit N --body-file <file>`.
- Finish by re-running the snapshot and showing the before/after line for "Loop next
  action", plus any flags that remain.

If a `gh` call fails partway through, stop and report exactly which changes landed. A
half-applied relabel can make the Loop select something unintended.
