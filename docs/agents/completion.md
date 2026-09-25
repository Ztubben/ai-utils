# Completion, Handoffs and failure — module notes

Notes for the plan → run completion stages (`lib/ralph_afk.py`, `lib/ralph_hil.py`), `lib/ralph_handoff.py`, `lib/ralph_failure.py`, and what counts as an Attempt. Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

- Stage completion that has side effects (merge/close/PR) follows a **plan → run** split:
  a pure planner returns the ordered git/gh commands as argv lists (unit-test the plan +
  its safety guards), and `run_plan` executes them fail-fast against git/gh on PATH
  (integration-test the CLI with mock `git`/`gh` scripts that log argv, prepended to PATH).
  `lib/ralph_afk.py` does AFK auto-merge: `afk_complete_plan` refuses (ok=False, no
  commands) when base is `main`, the story is not `type:afk`, or afk_merge is unknown;
  otherwise emits push → `gh pr create` (body `Closes #N`) → `gh pr merge --{method}` →
  `gh issue close`. `afk_merge` (merge|squash|rebase) maps 1:1 to the `gh pr merge` flag.
  Closing the issue is what makes `ralph_select` count the dep satisfied — the two connect
  through gh CLOSED state, not a shared call.
- `lib/ralph_hil.py` is the HIL sibling of `ralph_afk.py` (same `Plan`/`run_plan`/CLI shape):
  `hil_complete_plan` refuses when base is `main` or the story is not `type:hil`; otherwise
  emits push → `gh pr create` (body **Refs #N**, never `Closes #N`) → `gh issue edit
  --add-label state:awaiting-bench --remove-label state:in-progress`. It **never** emits a
  `gh pr merge` or `gh issue close`: the human bench-verifies and merges the clean diff. The
  issue therefore stays OPEN, so `ralph_select` keeps its dependents ineligible until a human
  closes it (bench-verified) — the inverse of the AFK path, and the key AC for US-007.
- `lib/ralph_handoff.py` is the checkpoint/resume seam (ADR-0004, Ralph never compacts):
  same `Plan`/`run_plan`/CLI shape. `handoff_plan` emits `git add -A` → `git commit
  --allow-empty` → `git push` the story branch → `gh issue comment` carrying
  `HANDOFF_MARKER` + summary (story stays state:in-progress, so selection resumes it).
  `resume_plan` refuses a non-`state:in-progress` story and emits `git fetch` +
  `git checkout <branch>`. Both refuse base/branch == `main`; neither references base,
  so the base branch is untouched. The comment marker is how a context-full checkpoint
  stays distinct from a failed Attempt: `non_handoff_comments` filters checkpoints out,
  and that is what US-009's attempt counter must operate on. The judgment-heavy "when to
  checkpoint / never compact" discipline lives in the checked-in prompt
  `prompts/handoff.v1.md` (drift-guarded).
- `lib/ralph_failure.py` is the failure-handling seam (US-009, ADR-0004): same
  `Plan`/`run_plan`/CLI shape. A failed **Attempt** is recorded as an issue comment
  carrying `ATTEMPT_MARKER`; `count_attempts` is built on
  `ralph_handoff.non_handoff_comments` so a checkpoint is never counted. `attempt_plan`
  posts one terse comment and, when the Attempt reaches `limits.max_attempts`, also
  emits `gh issue edit --add-label state:blocked --remove-label state:<current>`
  (`plan.blocked`/`plan.attempt_no` report the outcome). `circuit_breaker_plan`
  normalizes the backlog via `ralph_select.normalize`, counts open `state:blocked`
  stories, and when `>= limits.circuit_breaker` applies `needs-human` to the highest-
  numbered blocked story + tags `notify.github` — which halts the loop because
  `ralph_select` treats needs-human anywhere as HALT (tie AC "loop halts" back to
  select). CLI: `--record-attempt STORY REASON [CONFIG]`, `--check-breaker [BACKLOG]
  [CONFIG]`. The judgment-heavy "fail fast, don't thrash; re-attempt a kicked-back
  state:ready HIL story with a NEW failing test on a fresh PR" discipline lives in
  `prompts/failure.v1.md` (drift-guarded). `reset_on_block_plan` no longer rewinds
  anything (#75, PRD #69): with per-Story branches a blocked Story's work is quarantined
  from its siblings by construction, so the plan is push-its-own-branch → demotion comment
  naming that branch and the reason → `state:blocked`, never `--force`. An Orphan Story
  keeps the empty-plan behaviour and the caller's `attempt_plan` demotion.
- **Only a Handoff makes stopping short partial progress** (#95, ADR-0004): the tick counts the
  Story's Handoffs (`ralph --count-handoffs`, `ralph_handoff.count_handoffs`) before and after
  each iteration; a clean exit with no done-signal **and** no new Handoff is recorded with
  `--record-attempt`, so `limits.max_attempts` blocks it. Inside a review window,
  `await_review(..., record_attempt=)` is offered every retryable failure; the CLI charges an
  Attempt only for invalid output (exit 17 -- a model that answered nothing usable; an
  infrastructure failure, exit 12, stays a free retry per #61) via
  `ralph_failure.record_attempt`, and a blocking one ends the wait `BLOCKED` (exit **19**),
  which the tick handles like an escalation. Before this, autopilot_controller #72 ran 41
  handoff-less iterations and 15 empty reviews with nothing recorded.
  The same rule covers the other ways a run can stop short without being forgotten: a
  done-signal whose promotion fails (#98, tick), and a response refused as not append-only
  (#100, `respond_to_review` -> `ralph_failure.record_attempt`) each spend an Attempt. A
  session limit before the Story branch exists writes a comment-only Handoff (#99); the
  Handoff push names both refs and never uses `-u`. Every one of those paths asks
  `check_breaker` afterwards (`record_failed_attempt`, and a review step that failed): a
  Story blocked there counts toward `limits.circuit_breaker` (world
  `breaker_halts_after_failed_attempts`, PR #101 review).
