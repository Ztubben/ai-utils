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
  (loop back; the now-in-progress story is `resume`d next pass). Every knob is an env var so
  tests/superprojects override without editing the script. Covered by
  `test/bats/orchestration.bats` (run by `test/run.sh` when bats is present) AND
  `test/unit/test_orchestrate.py` (the executed gate here — bats is not installed — driving the
  script against mock `claude`/`gh`/`git` on PATH via `$RALPH_LOG` + a stateful `gh issue list`
  queue that pops one backlog fixture per call to simulate stories completing).
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
