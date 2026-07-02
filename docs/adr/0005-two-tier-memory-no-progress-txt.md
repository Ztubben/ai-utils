# Two-tier memory (Handoff + nested AGENTS.md); no progress.txt

Ralph keeps cross-iteration memory in exactly two stores, split by durability and scope, and does **not** use a `progress.txt` file.

- **Handoff** — transient, per-story. An issue comment (plus WIP on the story branch) holding resume state for the one in-flight story. Discarded when the story closes.
- **Learnings** — durable, global. Nested `AGENTS.md` files committed in the superproject, read at story start and updated when Ralph discovers a reusable convention, gotcha, or HAL pattern. Available across all features.

At story completion Ralph promotes reusable knowledge to the nearest `AGENTS.md` and leaves story-specific notes on the issue.

We diverge from the reference `snarktank/ralph`, which keeps a per-feature append-only `progress.txt` (archived per branch) plus project-wide `AGENTS.md`. We drop `progress.txt` because: (1) its audit-log job is already covered by git history, issue comments, and PRs; (2) its unit of scope is a whole PRD/branch, whereas our unit of work is a single small issue whose natural scratchpad is its own issue thread (the Handoff); and (3) an ever-growing append-only file re-read each clean-context iteration directly undermines the no-compaction budget (ADR-0004). `AGENTS.md` is kept lean and **nested** per module so each story reads only the relevant local file, not one growing global brain-dump. Trade-off: no single chronological narrative of the whole feature's progress in one file — reconstruct it from git + issue history if ever needed.
