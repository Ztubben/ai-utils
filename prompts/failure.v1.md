# Ralph Failure-Handling Prompt (v1)

When an **iteration** ends without its story reaching a green local gate — gating
red after honest effort, or you are genuinely stuck — that is a failed **Attempt**.
Ralph fails fast and loud rather than thrashing: record the Attempt, and let the
budget decide whether to block the story. Honor `CONTEXT.md` terminology — this is a
**HIL** (human-in-the-loop) loop; always use the term HIL.

## Recording an Attempt

Record one **terse** Attempt note on the issue via `ralph --record-attempt` with a
short reason (what failed, e.g. "gating red: test X"). Keep it to a sentence — the
note exists so the next iteration and the human can see the trail, not to re-explain
the whole story.

A context-full **checkpoint** (Handoff) is **not** an Attempt and must never be
recorded as one. If you are running low on context, write a Handoff and terminate
instead (see the handoff prompt); that does not spend the Attempt budget.

## Blocking a story

After `limits.max_attempts` (default 3) failed Attempts, `--record-attempt` moves
the story to **state:blocked** with one terse comment. A blocked story is set aside;
do not keep hammering it. Continue with other non-blocked eligible work — the
selection engine will hand you the next `state:ready`/`state:in-progress` story.

## Circuit breaker

Systemic failure shows up as *several* stories blocking. When a second story blocks,
`limits.circuit_breaker` (default 2) trips: run `ralph --check-breaker`, which halts
the whole loop, applies the **needs-human** label, and tags the configured handle so
the human investigates and resets. Once needs-human is present the loop halts — do
not start new work.

## Re-attempting a kicked-back story

A HIL story that failed its bench test is moved by the human back to **state:ready**.
When selection hands it to you again, treat it as a fresh re-attempt: write a **new
failing test** that captures what the bench revealed, drive it red → green, and open
a **fresh PR** off base for the human to bench-test again. Do not resurrect the old
PR or assume the previous diff was correct.
