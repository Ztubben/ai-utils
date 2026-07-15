---
name: to-issues
description: Break a plan, spec, or PRD into independently-grabbable issues on the project issue tracker using tracer-bullet vertical slices. Use when user wants to convert a plan into issues, create implementation tickets, or break down work into issues.
---

# To Issues

Break a plan into independently-grabbable issues using vertical slices (tracer bullets).

The issue tracker and triage label vocabulary should have been provided to you — run `/setup-matt-pocock-skills` if not.

## Process

### 1. Gather context

Work from whatever is already in the conversation context. If the user passes an issue reference (issue number, URL, or path) as an argument, fetch it from the issue tracker and read its full body and comments.

### 2. Explore the codebase (optional)

If you have not already explored the codebase, do so to understand the current state of the code. Issue titles and descriptions should use the project's domain glossary vocabulary, and respect ADRs in the area you're touching.

### 3. Draft vertical slices

Break the plan into **tracer bullet** issues. Each issue is a thin vertical slice that cuts through ALL integration layers end-to-end, NOT a horizontal slice of one layer.

Slices may be 'HIL' or 'AFK'. HIL slices require human verification on the physical bench
(hardware-coupled behavior CI cannot observe). AFK slices can be implemented and verified by
CI alone, without human interaction. Prefer AFK over HIL where possible.

<vertical-slice-rules>
- Each slice delivers a narrow but COMPLETE path through every layer (schema, API, UI, tests)
- A completed slice is demoable or verifiable on its own
- Prefer many thin slices over few thick ones
</vertical-slice-rules>

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each slice, show:

- **Title**: short descriptive name
- **Type**: HIL / AFK
- **Depends on**: which other slices (if any) must complete first
- **User stories covered**: which user stories this addresses (if the source material has them)

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the dependency relationships correct?
- Should any slices be merged or split further?
- Are the correct slices marked as HIL and AFK?

Iterate until the user approves the breakdown.

### 5. Publish the issues to the issue tracker

For each approved slice, publish a new issue to the issue tracker. Use the issue body template below. These issues are considered ready for AFK agents, so publish them with the correct triage label unless instructed otherwise.

Publish issues in dependency order (blockers first) so you can reference real issue identifiers in the "Blocked by" field.

<issue-template>
## What to build

A concise description of this vertical slice. Describe the end-to-end behavior, not layer-by-layer implementation.

Avoid specific file paths or code snippets — they go stale fast. Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it here and note briefly that it came from a prototype. Trim to the decision-rich parts — not a working demo, just the important bits.

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
- [ ] Criterion 3

Parent: #N               ← PRD issue number for Feature stories, or `Parent: None` for Orphan Stories
Depends on: #12, #34     ← or `Depends on: None`

</issue-template>

### Dependency rules

- **cross-Feature dependency prohibition**: a Feature story's `Depends on:` must reference only
  same-Feature stories, Orphan Stories, or PRDs — never a story belonging to a different
  Feature (its code lives on a different branch and is unreachable). Cross-Feature ordering is
  expressed as a `Depends on:` line between PRD issues; every Feature story inherits its PRD's
  unsatisfied dependencies (ADR-0006). Refuse to create a story dependency that violates this
  rule — propose the dependency on the PRD instead.

### Finishing the breakdown

When the breakdown is published from a PRD, apply `state:ready` to the PRD issue as the
final step. The `state:ready` label on the PRD is the gate that enables the completion pass
(ADR-0006): a Feature cannot be merged until its PRD carries `state:ready`, ensuring a
partially broken-down Feature is never merged early.

Do NOT close or modify the parent issue's body.
