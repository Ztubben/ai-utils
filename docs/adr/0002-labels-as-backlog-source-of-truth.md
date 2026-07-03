# Labels are the machine-readable source of truth for the backlog

Ralph reads the superproject's backlog from GitHub Issues. The authoritative, machine-readable encoding is **labels + issue-body conventions**, queried with `gh` over REST. A GitHub Projects v2 board, if used, is only a human-facing view derived from the labels — it is never what Ralph reads.

Encoding:
- **State** (mutually exclusive): `state:ready` → `state:in-progress` → `state:awaiting-bench` → closed (= Done).
- **Type**: `type:afk` / `type:hil`.
- **Priority**: explicit `prio:N` labels (lower = higher priority). Not issue-number ordering, so stories can be reprioritized without renumbering. Ties within a `prio:N` break by **lowest issue number (pure FIFO)** — deterministic and predictable, deliberately avoiding fan-out/AFK-first heuristics so Ralph never second-guesses the encoded priority.
- **Dependencies**: a `Depends on: #12, #34` line in the body; a story is ineligible until every referenced issue is Passing (closed). A HIL dependency counts as satisfied only once bench-verified.
- **Acceptance criteria**: a `## Acceptance Criteria` checklist. HIL stories also carry a `## Bench Test Procedure` section.

We chose labels over Projects v2 as the source of truth because label queries are cheap, REST-based, and low-context per tick, whereas reading Projects v2 status/priority and native "blocked by" edges requires GraphQL that is fiddlier and heavier for the agent to consume every 5 hours. The trade-off: state lives in flat labels rather than a richer board model, and the formatting skill must keep issues in exactly this canonical shape.
