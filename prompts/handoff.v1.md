# Ralph Handoff Prompt (v1)

Ralph **never compacts context**. Compaction silently degrades reasoning and hides
what was lost; instead, when a fresh-context **iteration** is running low on context
before its story is green, it writes a **Handoff** and terminates cleanly, and the
next iteration resumes the same story with clean context. Honor `CONTEXT.md`
terminology — this is a **HIL** (human-in-the-loop) loop; always use the term HIL.

## When to checkpoint

Watch your remaining context. As it fills — and before it runs out — **stop adding
new work** and write a Handoff. When a tool result carries a **Ralph context notice**,
your context has passed the configured hand-off threshold
(`limits.handoff_context_tokens`): finish the step you are on, then hand off. Do not try to compress or summarize the conversation
to keep going: a clean-context resume is always preferred over a degraded one. Size
stories small enough to fit one context window; terminate-and-resume is the safety
net when one still turns out too big.

## Writing the Handoff

A Handoff is durable state stored **only in the superproject** (never on the base
branch, never on `main`):

1. **Commit the WIP.** Commit everything you have on the story branch
   (`ralph/<issue#>-slug` from `branch_pattern`, `{issue}`/`{slug}` substituted) and
   push it, so the next iteration checks the branch back out. Do not merge, do not
   open a PR, do not close the issue.
2. **Post the Handoff comment** on the issue via `ralph --checkpoint`. It carries the
   handoff marker and your summary: what is done, what is left, the next concrete
   step, and any gotchas. The story stays at **state:in-progress** so selection
   resumes it first next iteration.
3. **Terminate.** End the iteration cleanly once the Handoff is written.

A context-full checkpoint is a normal boundary, **not** a failed **Attempt** — it does
not count against the story's attempt budget or trip the circuit breaker.

## When only a human can unblock the story

A Handoff is for work the *next iteration* can continue. If the story can go on only
once a human acts — a decision, an acceptance criterion restated (one that cannot be
met as written), a bench run — another iteration would just write the same Handoff
again. Commit and push what you have, then end the iteration with the needs-human
signal as the **last line** of your output, saying exactly what the human must do:

```
RALPH-NEEDS-HUMAN: restate AC 3 with a tolerance the hardware can reach
```

Ralph then parks the story on `needs-human` and tags the human with that line. It is
not an Attempt, the story is never `state:blocked`, and the loop goes on with other
work until the human removes the label. Do not use it for anything the next
iteration could do itself.

## Resuming

The next iteration runs `ralph --resume`: it checks out the story branch and surfaces
the latest Handoff comment. Read that Handoff, pick up from the next concrete step, and
continue red → green from the story's `## Acceptance Criteria`. Do not restart the
story from scratch or recreate the branch.
