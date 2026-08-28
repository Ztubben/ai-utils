# ai-utils — Ralph Loop tooling (module notes)

The tool being built here is the **issue/label-driven Ralph Loop** shipped from
ai-utils. (The snarktank-style loop in `ralph/` is only the *build harness* that
drives construction — don't confuse the two.) Honor `CONTEXT.md` terminology (HIL,
not HITL) and `docs/adr/0001–0005`.

## Layout
- `bin/ralph` — bash CLI entrypoint; dispatches subcommands, delegates logic to `lib/`.
- `bin/ralph.sh` — the unattended **tick** loop (orchestration only; the scheduler runs it).
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
  `Action`). It reuses `ralph_story`'s field extraction but owns ordering (optional prio
  ascending — absent prio sorts last — ties by lowest issue number, FIFO) and dependency
  satisfaction. The scan must request the
  gh `state` field. Active Stories (`state:in-progress` or `state:in-review`) resume
  before any Ready Story starts. A `Depends on:` edge is satisfied only when the
  referenced issue is
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
- `lib/ralph_models.py` is the model-profile seam (#44, PRD #42): pure logic, **no**
  `Plan`/git/gh (resolution decides, it does not mutate). `profiles(config)` reads the
  catalog into `{key: ModelProfile(key, provider, model)}`; `resolve_roles(config,
  implementation=, review=, allow_same_model=)` layers a per-role override over the
  committed `models.defaults` and returns a `RoleResolution`. `same_identity` compares the
  **exact configured model identifier only** — the provider adapter is an internal concern,
  so one model reached through two adapters is still one model and the pair is refused
  unless the operator acknowledges it. CLI: `--resolve-models [CONFIG] [--implementation
  KEY] [--review KEY] [--allow-same-model]` (exit 2 on bad config or refusal). GOTCHA: the
  catalog is a **list** of `{key, provider, model}`, not a mapping — a YAML mapping would
  silently swallow a duplicate profile key, and rejecting duplicates is an AC. Catalog
  well-formedness (unknown adapter via the schema enum; duplicate key and dangling role
  default via `ralph_config._model_catalog_errors`) lives in `ralph_config` so
  `--check-config` stays the one place a broken catalog is reported; the dependency runs
  `ralph_models` → `ralph_config` only, never back. `models:` is optional in the schema (a
  target repository opts in), so every pre-existing config still validates — but note
  `_apply_defaults` materializes an empty `models.defaults` for configs that omit it, which
  is why the cross-field checks run on the raw data and `profiles()` treats absent/empty as
  an empty catalog.
- `lib/ralph_review.py` owns the exact durable pull-request opt-in marker and
  `is_managed_pr`; an unmarked PR is never eligible for automated review. Locally green
  implementation promotion (#49) lives in `lib/ralph_implementation.py`: its pure
  `implementation_green_plan` pushes the resolved Story/Feature branch, creates a marked PR
  or updates the already-open marked PR, and moves both AFK and HIL Stories to
  `state:in-review`. It never merges, closes, or emits `state:awaiting-bench`, and refuses
  `main` and an unmarked existing PR. `bin/ralph.sh` calls `--implementation-green` for the
  done signal; the older AFK/HIL completion modules are no longer implementation-green paths.
- `lib/ralph_review_context.py` builds the diff-first, commit-bound evidence bundle for one
  Negotiation Round (#50, PRD #42): pure `build_context(...) -> ContextResult` plus the
  `--review-context STORY PR ROUND [ROOT]` CLI (exit 2 on incomplete evidence). It refuses
  unless the PR carries `ralph_review`'s managed marker and an exact `baseRefOid`/`headRefOid`,
  and the CLI `rev-parse`s the head and asserts it resolves to itself **before** reading the
  base/head diff — a bundle bound to a moving ref would have the reviewer judging a head that
  no longer exists. GOTCHAS: (1) the bundle is evidence, never a Handoff — `durable_discussion`
  admits only the *pull request's* comments and reviews, because Story comments are exactly
  where Handoffs, Attempts, and implementation session notes live; `HANDOFF_MARKER` bodies are
  dropped on top of that as belt-and-braces. (2) `_section` strips the trailing canonical
  `Parent:`/`Depends on:` metadata off the Acceptance Criteria — that is selection state, not
  acceptance evidence. (3) `repository_evidence` is deliberately **scoped, not the checkout**:
  root `AGENTS.md` plus every `AGENTS.md` nearest-first up from each changed path, then
  `CONTEXT.md` + `docs/adr/*.md` — an unrelated sibling directory never enters the prompt (the
  reviewer reads the rest of the tree itself, read-only). (4) The read-only half of the AC is
  **not** in this module and is not asked for in the prompt: `ralph_agent` adapters take a
  `role`, and `role="review"` launches `claude --safe-mode --permission-mode plan
  --no-session-persistence` / `codex exec --sandbox read-only --ephemeral
  --ignore-user-config` instead of the implementation role's bypass flags, and strips
  `GH_TOKEN`/`GITHUB_TOKEN`/`GH_ENTERPRISE_TOKEN` from the child env. Enforced at launch, so a
  model that ignores its instructions still cannot mutate the checkout or reach GitHub — which
  is what CONTEXT.md's Review Agent ("is read-only, and holds no GitHub credential") promises.
- `lib/ralph_review_result.py` + `schema/review.schema.json` are the versioned
  structured-review contract and its validator (#51, PRD #42): pure
  `changed_lines(diff)` / `validate_review(payload, changed=, raw=) ->
  ReviewValidation` plus the `--validate-review PAYLOAD [DIFF]` CLI (0 postable,
  1 refused naming field paths, 2 unreadable input, like `--lint-story`). The
  contract itself — every field, the blocking policy, the size rationale — is
  documented once in `docs/review-contract.md`; a drift-guard test asserts the doc
  still names the version, the fields and both category lists. GOTCHAS: (1) the
  validator **refuses whole, never repairs** — this is the only path a read-only,
  credential-less Review Agent's judgment takes to a pull request. (2) `blocking`
  is required with **no default** and must agree with `category` (six blocking
  reasons, three non-blocking), and the verdict must agree with the findings:
  `request_changes` iff at least one blocker. Inferring blocking from a category
  alone would let a preference block delivery. (3) `changed_lines` counts context
  lines inside a hunk, not just added ones, because that is exactly what GitHub
  accepts an inline thread on; removed lines are excluded (they do not exist at
  the reviewed head). (4) The size guard runs **before** schema validation and
  measures `raw` when given, so an oversized payload is rejected as bytes rather
  than as fifty field-level errors; the cap (60000) is GitHub's 65536-character
  review body minus Ralph's framing. (5) `MAX_FINDINGS` and the schema's `maxItems`
  are asserted equal by a test — keep them in step. (6) Error rendering is
  `ralph_config.format_error` (renamed from private for this reuse), so both
  validators name the offending field path identically.
- `lib/ralph_review_render.py` turns a validated result into ordinary GitHub review
  artifacts (#52, PRD #42): pure `review_body` / `inline_comments` / `review_payload` /
  `check_command` / `render_plan(result, pr, payload_path) -> Plan`, plus `run_plan` and
  the `--render-review REVIEW PR [DIFF]` CLI (0 posted, 1 gh failure, 2 refusal).
  GOTCHAS: (1) the review is **always** `event: COMMENT` — GitHub refuses APPROVE and
  REQUEST_CHANGES on a pull request the same account authored, and Ralph's PRs are
  opened with the operator's own credential, so a verdict-shaped event would 422 exactly
  when it matters. The verdict rides on the one stable commit-status context
  `ralph/model-review` (request_changes → failure, otherwise success), which is what a
  target repository requires in branch protection. (2) A **stale head refuses before any
  request**: nothing posted, check untouched, both commits named — stale findings
  describe code that is no longer there. Same for an unmarked PR. (3) The CLI re-runs
  `ralph_review_result.validate_review` at the posting site, so the #51 gate holds even
  if a caller skips `--validate-review`; pass DIFF and a location the diff never touched
  is caught here rather than as a whole-review 422. (4) Inline threads anchor on the new
  side: a range is `start_line`..`line` (GitHub addresses a multi-line thread by its last
  line), and a single-line finding sends **no** `start_line` — `start_line == line` is
  rejected. (5) The nested `comments[]` body cannot be expressed with `gh api -f`, so the
  CLI writes the payload to a temp file and the plan carries `--input PATH`; the plan
  stays a pure argv list and `run_plan` is unchanged (contrast: do not teach the shared
  Plan/run_plan shape about stdin for one caller).
- Durable **model assignment** on the story (#46, PRD #42) lives in two halves.
  `lib/ralph_story.py` owns the *shape*: `MODEL_LABEL_PREFIXES` (`model:impl:` /
  `model:review:`), `model_label(role, model)` and `model_assignment(story) -> (dict,
  errors)`, with two labels for one role an ambiguity error and the assignment surfaced as
  `fields["models"]`. `lib/ralph_models.py` owns *policy*: `roles_for_story` resolves each
  role from the story's label when it has one and from the catalog when it does not
  (`resolution.newly_assigned` names the fresh choices), and `assign_plan` returns the
  ordered gh plan to create each new identity's label on demand and apply both in one
  `issue edit`. CLI: `--assign-models STORY [CONFIG] [--implementation KEY] [--review KEY]
  [--allow-same-model]`. GOTCHAS: (1) the labels record the **exact model identity, never
  the profile key** — keys are config-local and can be re-pointed, so only the identity
  makes a retry reproducible; `profile_for_model` maps the identity back to a provider
  adapter and refuses an identity that has left the catalog (the allowlist still governs
  what may launch). (2) The same-model refusal guards **fresh choices only** — a pair
  already persisted is honored as recorded, because re-litigating independence on every
  resume would strand an in-flight story on an unrelated config change. (3) A half-assigned
  story (a crash between the two label writes) heals **forward**: only the missing role is
  recorded, the existing one is never rewritten. (4) With no `models:` catalog `assign_plan`
  is a documented **no-op** (`implementation is None`), not a refusal, so a target repository
  that opted out keeps ticking. `bin/ralph.sh` calls `assign_story_models` on both `start`
  and `resume` (best-effort; idempotent, which is what makes the resume call safe and lets a
  story started before it had an assignment heal forward), and `run_iteration` passes the
  fetched story to `ralph --launch-agent ... --story FILE` so the launch reads the
  assignment rather than the current defaults. NOTE: `ralph_init.label_command` is the one
  idempotent `gh label create --force` spelling — assignment labels are created on demand
  (one per identity) rather than seeded by `ralph --init`, but reuse it.
- **Role alternation** (#47, PRD #42) lives in `lib/ralph_alternation.py`: pure ordering
  (`order_for(phase, impl, review)`, `swaps`, `advanced`, `enabled(config)`) plus a tiny
  state store (`state_path` / `read_phase` / `write_phase`). `assign_plan(..., phase=,
  fixed_roles=)` stays pure — it takes the phase, reports `plan.swapped` and
  `plan.advances_alternation` — and `_cmd_assign` is the only place that reads the phase
  and advances it. GOTCHAS: (1) alternation applies to a **fresh pair only** (both roles
  newly assigned). A resume, a retry, a further review round, and a half-assigned story
  healing forward all keep what the story carries and leave the phase alone — otherwise a
  resume would consume the swap the next new story is owed, or worse, swap a story's model
  midway. (2) The phase advances **after** `run_plan` succeeds, so a gh outage cannot burn a
  swap. (3) The phase is loop-local state under the target repository's git dir (next to the
  tick lock, `state_path` follows a `gitdir:` gitlink), never the working tree and never the
  backlog; a missing/damaged file or a checkout with no git dir degrades to "start over" /
  "never alternate" rather than failing a tick — it is a balance heuristic, not a
  correctness invariant. (4) The swap happens before the labels are built, so what the story
  records *is* the alternated order and every later stage reads it from #46's labels; the
  same-model refusal is unaffected (a pair is one identity either way round). Fixed roles:
  committed `models.alternate: false` (schema default true) or `--fixed-roles` on
  `--assign-models` only — `--resolve-models` rejects the flag rather than ignoring it.
- **Test harness gotcha**: the tick harnesses (`test/unit/test_orchestrate.py`,
  `test/unit/test_freshness.py`, `test/bats/orchestration.bats`) inherit the ambient
  environment. A Ralph iteration exports the provider binary overrides (`RALPH_CLAUDE` /
  `RALPH_CODEX`), which beat the fakes on `PATH` (`AgentAdapter.binary()` prefers
  `binary_env`) and make the suite launch a **real** agent — a hang, not a failure. Each
  harness therefore strips every adapter's `binary_env` from the child env; keep that when
  adding a harness that runs `bin/ralph.sh`.
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
- `bin/ralph.sh` is the unattended **tick** (US-011, ADR-0002/0004/Tick): thin orchestration —
  the TDD/gating happen inside the `claude` iteration it launches (driven by
  `prompts/iterate.v1.md`), but the tick owns the state transitions around it. Order: (1)
  `flock -n` a lockfile under `.git/` (`RALPH_LOCK_DIR`, default `.git`) so only one tick per
  superproject runs — an overlapping tick logs "already running" and exits 0; (2)
  `ralph --check-config` (fail loud, ADR-0001); (3) loop calling `ralph --dry-run` (already
  resume-first) and launching one fresh-context `claude --print` iteration per selected story,
  working stories in sequence until `--dry-run` returns `no-work`/`halt`. A `start` action
  first moves the story `state:ready`→`state:in-progress` (`begin_story`, the state-machine
  `start` edge every later stage assumes); `resume` is left as-is. `run_iteration` returns
  three outcomes: session-limit (checkpoint via `ralph --checkpoint -` and end), the story
  done-signal marker `RALPH_STORY_COMPLETE_MARKER` (**promote**: `complete_story` reads the
  `type:` label and dispatches to `ralph --complete-afk`/`--complete-hil`), or partial progress
  (loop back; the now-in-progress story is `resume`d next pass). GOTCHA (tick spin) — "partial
  progress" is `rc == 0` **only**. A launcher that refuses (bad config, refused role
  resolution, or a `ralph` that does not know the subcommand — exit 2) never ran an agent, so
  the same call fails identically on the next pass; it returns `RC_LAUNCH_UNAVAILABLE` (13) and
  ends the tick non-zero. Do not fold it into `RC_INFRA_FAILURE` (12), which means the provider
  *did* start and may have done work before dying — that one legitimately resumes on a later
  pass. Treating a refusal as progress is how a tick spun to `RALPH_MAX_ITERATIONS` launching
  nothing and still exited 0 (2026-08-28): the checkout had moved to a branch whose `ralph`
  predated `--launch-agent`. `$RALPH_BIN` is re-read from disk on every call and a tick checks
  out branches, so the tick now preflights the subcommands it needs right after
  `--check-config`, *before* `begin_story` can label anything — that tick left #48 stranded in
  `state:in-progress`. Every knob is an env var so
  tests/superprojects override without editing the script. Covered by
  `test/bats/orchestration.bats` (run by `test/run.sh` when bats is present) AND
  `test/unit/test_orchestrate.py` (the executed gate here — bats is not installed — driving the
  script against mock `claude`/`gh`/`git` on PATH via `$RALPH_LOG` + a stateful `gh issue list`
  queue that pops one backlog fixture per call to simulate stories completing).
- `lib/ralph_session.py` owns the one question the tick must never get wrong (#65): did the
  launched agent hit its **session limit**, or not? Reached from bash via
  `ralph --classify-session RC` (agent output on stdin, verdict as the exit code —
  `EXIT_SESSION_EXHAUSTED` is 10, the tick's own `RC_SESSION_LIMIT`). It replaced a single exit
  code (91) + a single literal (`"usage limit reached"`) that the claude CLI stopped emitting:
  the miss made `run_iteration` return partial-progress, so the tick relaunched the same story
  until `RALPH_MAX_ITERATIONS` ran out (a live retry-storm on 2026-08-27). Detection is layered
  on purpose — an exit-code **set** (`RALPH_SESSION_LIMIT_EXIT` takes a comma list), a **family**
  of wording regexes matching the *shape* of a limit notice, and an **additive**
  `RALPH_SESSION_LIMIT_MARKER` (it can widen but never replace the built-ins; a replaceable
  marker is how one stale literal became the only detector). GOTCHA — the counter-hazard is the
  substring rule biting again: the tick greps the agent's whole transcript, and an agent working
  *on this code* writes "session limit" in prose constantly. So a match only counts on the final
  non-empty line (or the last 3 when the process also failed) — a provider's notice is
  *terminating* output; the agent's prose is not. Covered by `test/unit/test_session_limit.py`
  plus tick-level tests in `test_orchestrate.py`/`orchestration.bats`.
  `AgentAdapter.classify()` in `lib/ralph_agent.py` calls straight into this module and adds
  only the third outcome the module leaves to its caller (a dirty exit is infrastructure).
  It must never re-derive the limit verdict: the duplicate copy it once carried went stale
  against the CLI wording exactly the way the original literal did.
- GOTCHA (tick tests) — the tick test harnesses (`test_orchestrate.py`, `test_freshness.py`,
  `orchestration.bats`) mock `claude`/`gh`/`git` on PATH but build their env from
  `os.environ`. When the suite is run *from inside a Ralph iteration* (the normal case — Ralph
  works its own repo), that environment already carries `RALPH_CLAUDE=/path/to/real/claude` and
  `RALPH_ITERATION_*` from the tick that launched the agent. Inherited, they make the
  tick-under-test launch the **real** claude CLI: a nested agent session, tests that hang for
  minutes and then fail with zero mock-claude calls, and the nested session's own `git` calls
  polluting `$RALPH_LOG`. The harnesses now pin `RALPH_CLAUDE=claude` (so PATH resolves the
  mock) and strip the inherited `RALPH_*` knobs. Any new harness that shells out to `bin/ralph.sh`
  must do the same.
- `lib/ralph_init.py` is the one-shot bootstrap seam (`ralph --init`): same pure-`Plan`/
  `run_plan`/CLI shape as the completion stages. `init_plan(base, base_exists, default_branch,
  prio_max)` returns the ordered gh/git commands to `gh label create --force` the canonical
  vocabulary (`FIXED_LABELS` — the exact `state:`/`type:`/`needs-human`/`ready-for-human`
  labels `ralph_story`/`ralph_select` consume — plus a `prio:0..N` starter range) and, when
  `base` is missing, create it off the default branch (refusing to fabricate `main`, ADR-0001).
  The CLI detects live state (`_remote_has_branch`, `_default_branch`) then runs the plan.
  Idempotent by construction (`--force`, skip-if-present). Covered by `test/unit/test_init.py`.
- `scheduler/` ships the sample schedulers (US-012, ADR-0001): systemd `ralph.service`
  (`Type=oneshot`, `ExecStart=.../bin/ralph.sh`) + `ralph.timer` (`OnCalendar=*-*-* 00/5:00:00`,
  i.e. every 5h) and a `ralph.cron` one-liner (`0 */5 * * *`). Both just fire the flock-guarded
  tick. Green gate is a DRIFT-GUARD (`test/unit/test_scheduler.py`), same idea as the prompt
  tests: assert the files exist and still carry the 5-hour cadence + `ralph.sh` ExecStart, and
  that the README's install section covers submodule/config/schedule/`gh auth login`+`claude`
  auth. GOTCHA: do NOT `assertNotIn("HITL", README)` — the README intentionally says "never
  *HITL*", so that check false-positives (the substring rule bites again, cf. the prompt note).
- `test/unit/test_terminology.py` is the repo-wide **docs/terminology guard**: CONTEXT.md's
  `## Language` section is the single glossary (parsed from its `**Term**:` headings), and no
  shipped surface (README/CONTEXT/AGENTS, `docs/`, `prompts/`, `skills/`, `bin/`, `lib/`,
  `schema/`) may contradict it. `ralph/` (the build harness) and `test/` are excluded. It judges
  **paragraphs and sentences, not lines**, because markdown claims wrap; that is what makes the
  stale-invariant check work at all (a guard describing its own forbidden phrasing would trip it). Two escape hatches, both deliberate: a never-`ai-utils`
  sentence is fine when it scopes itself to the submodule mount or names the target repository
  (ADR-0001 amendment), and a doc may spell HITL only in a forbidding context (`never HITL`,
  `_Avoid_: HITL`). New Feature vocabulary goes in CONTEXT.md **and** in this guard's term list.
