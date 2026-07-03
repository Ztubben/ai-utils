# Ralph never compacts context: terminate-and-resume with a two-level loop

The Ralph Loop must never compact context. When an agent iteration's context fills, it terminates itself after writing a Handoff (a summary of work done plus the code implemented so far), and the next iteration resumes the same story with clean context. This makes story sizing important: stories should be small enough to fit within a single context window. The issue-formatting skill nudges toward this **advisorily** — an interactive proxy checklist (>~3–5 acceptance criteria, >1 module, HAL-seam-plus-logic bundled, or a slice that needs an "and" to describe) that suggests splits the human approves; it is not a hard gate and there is no token estimator. Terminate-and-resume is the safety net for any story that still turns out too big.

The loop has two levels:
- **Tick** — a scheduled run every 5 hours (local scheduler), bounded by the Claude session window. Works as many eligible stories as budget allows.
- **Iteration** — a fresh-context agent process inside a tick. Ends either by completing a story, by checkpointing when context fills (Handoff, resume next iteration), or by failing.

Failure handling:
- A story gets a configurable number of failed **Attempts** (default 3; a context-full checkpoint is not an Attempt). After the limit, the story moves to `state:blocked` with one terse comment, and Ralph looks for other non-blocked eligible work.
- **Circuit breaker:** if a second story also fails to `state:blocked`, Ralph halts the whole loop and notifies the user to reset, on the assumption that something systemic is wrong.

We chose terminate-and-resume over context compaction because compaction silently degrades reasoning quality and hides what was lost; a clean-context resume with an explicit Handoff is auditable and keeps each iteration sharp. The trade-off is that stories must be genuinely small and a durable Handoff mechanism is required.
