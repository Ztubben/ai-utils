# ai-utils — Ralph Loop tooling (module notes)

The tool being built here is the **issue/label-driven Ralph Loop** shipped from
ai-utils. (The snarktank-style loop in `ralph/` is only the *build harness* that
drives construction — don't confuse the two.) Honor `CONTEXT.md` terminology (HIL,
not HITL) and `docs/adr/0001–0005`.

## Layout
- `bin/ralph` — bash CLI entrypoint; dispatches subcommands, delegates logic to `lib/`.
- `lib/*.py` — pure logic (Python 3, stdlib + `jsonschema` + `PyYAML`). No network, no side effects.
- `schema/*.json` — shipped JSON-schemas (e.g. `ralph.schema.json` for `.ralph.yml`).
- `skills/*/SKILL.md` — authoring skills shipped with the tool (e.g. `ralph-story`, which
  specializes `to-issues` to emit the canonical backlog shape). A skill's `examples/` hold
  well-formed sample issues that a test asserts stay canonical.
- `.ralph.yml.sample` — documented sample config that MUST validate (a test asserts it).
- `test/run.sh` — the green gate. `test/unit/` = Python `unittest` (fixtures under `test/fixtures/`); `test/bats/` = bats orchestration (auto-skipped if bats absent).

## Conventions / gotchas
- No `pytest`/`bats` installed here; unit tests use stdlib `unittest`, run via `test/run.sh`.
- Python logic returns a result object (`ok`, `errors`, resolved data) rather than
  exiting; only the CLI wrapper prints and sets exit codes. Keeps logic unit-testable.
- Error strings name the offending field path (e.g. `branching/afk_merge: ...`) so
  `--check-config` failures are actionable.
- Config validation is JSON-schema Draft-7 with `additionalProperties: false`, which is
  how the mandated label scheme stays non-overridable (unknown keys like `labels:` fail).
- Schema `default`s are applied by `lib/ralph_config.py` after validation (jsonschema
  does not fill defaults itself).
- A "story" is a GitHub issue in `gh issue view --json number,title,labels,body` shape
  (labels as `{"name": ...}` objects); `lib/ralph_story.py` normalizes labels (accepts
  objects or plain strings) and is the canonical story-format checker the selection engine
  builds on. Fixtures for story-shaped logic live under `test/fixtures/stories/`.
- `lib/ralph_select.py` is the pure selection engine (`normalize` → `select_next` →
  `Action`). It reuses `ralph_story`'s field extraction but owns ordering (prio ascending,
  ties by lowest issue number, FIFO) and dependency satisfaction. The scan must request the
  gh `state` field: a `Depends on:` edge is satisfied only when the referenced issue is
  closed (an AFK dep once merged, a HIL dep once bench-verified — both surface as closed).
  Don't confuse gh's `state` (OPEN/CLOSED) with the `state:` label (ready/in-progress/…).
  Backlog fixtures (JSON arrays of gh-shaped issues) live under `test/fixtures/backlogs/`.
- `lib/ralph_iterate.py` holds the deterministic seams of one iteration: `branch_name`/
  `slugify` (pure — story branch from `branch_pattern`, `{issue}`/`{slug}` substituted) and
  `run_gating` (shells the configured steps in order, fail-fast, captures stdout+stderr,
  returns a `GatingResult`). `--run-gating` is low-verbosity: passing steps print only a
  check line, a failing step's output goes to stderr. The judgment-heavy TDD itself lives
  in the checked-in **agent prompt** `prompts/iterate.v1.md`; a unit test drift-guards its
  required directives (red/green, off-target HAL, gating, `{issue}`/`{slug}`, never touch
  base/main, HIL not HITL). Gating-config fixtures live under `test/fixtures/gating/`.
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
  `prompts/failure.v1.md` (drift-guarded).
- `lib/ralph_memory.py` is the two-tier memory seam (US-010, ADR-0005): pure filesystem
  queries, **no** `Plan`/git/gh (nothing to mutate — memory is just files). `nested_agents_md
  (start_dir, root)` returns the `AGENTS.md` to read at story start, nearest-first from
  `start_dir` up to and including `root`; `promotion_target(changed_path, root)` returns the
  nearest existing `AGENTS.md` to promote a learning to, and when none exists in the chain it
  keeps the learning **module-local** by targeting a new `AGENTS.md` in the changed file's own
  directory (not the root). `is_progress_txt`/`find_progress_txt` guard ADR-0005's "no
  progress.txt". CLI: `--read-learnings DIR [ROOT]` (exit 2 if DIR missing), `--learn-target
  PATH [ROOT]`. The judgment-heavy discipline (read nearest-first at start; promote reusable,
  keep lean/module-local; story-specific notes go on the issue, not AGENTS.md) lives in
  `prompts/memory.v1.md` (drift-guarded). NOTE: the reference snarktank loop's `progress.txt`
  is the build harness in `ralph/`, which is deliberately separate from the tool being built —
  the tool ships no progress.txt.
