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
- `lib/ralph_review_round.py` is round one end to end (#53, PRD #42): pure `conduct(story,
  pr, context, launch, publish, changed=)` — dedupe, launch, extract, validate, publish —
  plus `next_round`, `review_prompt`, `extract_result`, live `discover_pull_request`, and
  the `--review-round STORY [CONFIG] [ROOT] [--pr PATH]` CLI. The judgement half is the
  checked-in `prompts/review.v1.md` (drift-guarded for fresh context, the read-only/no-
  credential boundary, the six blocking + three non-blocking categories, and the evidence
  fields). GOTCHAS: (1) "exactly one review per head commit" is enforced by a **durable
  marker on the review itself** — `ralph_review.review_marker(head)` is prepended to #52's
  `review_body`, and `reviewed_heads`/`is_reviewed` read it back off the PR. The fact lives
  where the review does, so a fresh clone, another machine, or a later tick all agree;
  loop-local state would re-review and re-spend an invocation. `next_round` counts those
  markers, so a human review never advances a Negotiation Round. (2) Both refusals — an
  unmarked PR, and a head already reviewed — are checked **before** any evidence is
  assembled, so they cost zero invocations (and no git work). (3) A provider that died is
  reported with the adapter's own outcome string, never as `invalid-output`: #61 must be
  able to tell "the reviewer never finished" from "the reviewer said something
  unpublishable", and `EXIT_CODES` keeps `--launch-agent`'s codes (10/12) for exactly that.
  (4) `extract_result` recovers the contract object from the provider's prose (last balanced
  top-level `{...}`, fences included) but **never repairs** it — the #51 validator still sees
  it whole. (5) Discovery matches the marker **plus** `Refs #N` from #49's PR body, never a
  branch name or title, and refuses on more than one match. (6) The two seams this reuses
  were extracted for it: `ralph_review_context.bundle_for` (bundle + the diff again, which
  the round needs for `changed_lines`) and `ralph_review_render.publish` (whose
  `PublishResult.failed` is what separates a gh failure, exit 1, from a refusal, exit 2).
  `bin/ralph.sh` calls `review_round` where it used to park an In Review Story, then still
  parks — #54 replaces that park with the bounded in-tick wait.
- `lib/ralph_review_respond.py` answers a round that requested changes (#55, PRD #42):
  pure `respond_prompt`, `validate_response(payload, result)`, `append_only_errors`,
  `reply_commands`, `response_comment`, plus `conduct(..., launch, publish, checkout)`,
  the live `respond_to_review(story, pr, config, root)` and the `--respond-review STORY
  [CONFIG] [ROOT] [--pr PATH]` CLI. Contract: `schema/response.schema.json`
  (`ralph-response/v1`), judgement half in the drift-guarded `prompts/respond.v1.md`.
  GOTCHAS: (1) append-only is **verified, not trusted** — `git merge-base --is-ancestor
  <reviewed head> <new head>`. An amend, a rebase and a force-push all fail exactly that
  test, and each would strand the review threads, the checks and the commit evidence
  citing the reviewed commit. An `accepted` disposition with an *unchanged* head is
  refused for the mirror-image reason: a claimed fix with no commit behind it. (2) Ralph
  does the `git push` (never `--force`) in `publish`, before the replies: until the fix is
  on the remote there is no new head for CI and the next round to judge, and the replies
  would describe commits nobody else can see. (3) The answer must cover every open finding
  exactly once and none that was never raised — a partial answer would leave the loop
  unable to say whether a blocker was handled. (4) Threads are matched by the rendered
  heading `**F-1**` at the *start* of #52's comment body, so a finding id quoted inside
  someone's prose cannot claim a thread; a cross-cutting finding has no thread and is
  answered by the consolidated record alone. (5) The reply endpoint needs the pull number
  (`repos/{owner}/{repo}/pulls/{n}/comments/{id}/replies`) — the shorter spelling 404s.
- **Disputes and later-round scope** (#56, PRD #42) are spread across the modules they
  belong to, not a new one. `schema/response.schema.json` gains a third disposition,
  `disputed`, with a conditional `required: [evidence]` (Draft-7 `if`/`then`) — a dispute
  with no evidence is an assertion, which is exactly what the finding contract forbids of
  the reviewer. `ralph_review_respond.disposition_body` is the one rendering of an
  answered finding, used by both the thread reply and the consolidated comment, so
  evidence cannot reach one and not the other. GOTCHAS: (1) `append_only_errors` now
  refuses in **both** directions — an `accepted` finding with an unchanged head (a fix
  nobody made) *and* a moved head with nothing accepted (a change nobody asked for). The
  second is what makes "a dispute changes no code" an invariant rather than an
  instruction. (2) "One review per head" became "one review per *unanswered* head":
  `ralph_review.needs_review(pr, comments)` compares `rounds_reviewed` (review markers
  stamping that head) against `rounds_answered` (recorded responses for it), so one
  answer buys exactly one re-review and a model that only ever disputes cannot spin the
  PR through unlimited invocations. `review_stamps` is therefore a **list**, and
  `next_round` counts stamps, not distinct heads — counting heads would hand a disputed
  round the number it just used, making a round limit unreachable. (3) `next_step`'s
  RESPOND arm no longer asks "is there any response for this head" (a second changes-
  requested round at one head would read as settled); the same reviewed-vs-answered
  comparison decides both arms. `respond_to_review` carries the mirror guard so
  `--respond-review` by hand cannot double-answer. (4) The bundle carries the negotiation
  verbatim for round ≥ 2 (`ralph_review.negotiation_history` — the *only* Story-comment
  records admitted, because they **are** the negotiation), but `for_role` gates the scope
  directive: handing the Implementation Agent instructions written for whoever judges it
  is the bug that parameter prevents. (5) AC5 is enforced in the validator, not left to
  the prompt: `validate_review(..., prior_findings=)` refuses a blocker whose id is new in
  round ≥ 2 unless its category is in `LATE_BLOCKING_CATEGORIES` (`defect`,
  `safety_regression`). `prior_findings=None` means "unknown, do not restrict"; `[]` means
  "nothing was raised" and does restrict. (6) Withdraw/uphold is **derived, not declared**:
  `ralph_review_result.adjudicate(prior, current)` reads it off which identifiers a round
  restates, and `review_body(..., prior_findings=)` renders it under "## Earlier findings"
  so a withdrawal is a visible decision instead of a silence.
- `lib/ralph_review_deadlock.py` is the end of automated negotiation (#57, PRD #42):
  pure `unsettled(comments) -> [Unsettled]`, `escalation_comment`, `escalate_plan` (the
  usual Plan/`run_plan` shape, borrowing `ralph_review_render`'s), the live
  `escalate(story, pr, config, root)` and the `--escalate-review STORY [CONFIG] [ROOT]
  [--pr PATH]` CLI. `review.max_rounds` (schema default **2**) is the budget;
  `WaitPolicy.from_config` carries it and `next_step(..., max_rounds=)` returns the new
  `ESCALATE` when a move is owed and the budget is spent, which ends the wait with exit
  **15**. GOTCHAS: (1) escalation is **one Story's** ending, never the loop's — the plan
  is asserted to contain no `needs-human`. The tick calls `--check-breaker` right after
  escalating, so a *pattern* of deadlocks still halts the loop through the existing
  counter (`limits.circuit_breaker`), which the newly `state:blocked` Story now counts
  toward. Escalating and halting are deliberately two decisions in two places. (2) The
  tick `continue`s after an escalation instead of returning: the Story is blocked, so
  resume-first will not pick it again, and unrelated ready work proceeds in the same
  tick. (3) `unsettled` reads the **last round's blocking findings** — a withdrawn
  finding is simply absent, and a non-blocking remark never deadlocked anything — and
  attaches *every* answer each one received, with the round number folded in, because a
  dispute that held across two rounds reads differently from one made late. (4) The
  comment carries both cases in full on the pull request; arbitration that needs the
  thread history reconstructed first does not happen. (5) `discover_pull_request` now
  also fetches `repos/{owner}/{repo}/pulls/N/comments` and attaches it as
  `reviewThreads`, and `durable_discussion` admits it as `thread_reply` — that is where
  a human answering a finding directly before escalation is recorded, and the next fresh
  round must not re-litigate what a human already settled. `_author` therefore reads
  `user.login` (REST) as well as `author.login` (`gh --json`).
- `lib/ralph_review_human.py` is human arbitration through GitHub's own controls (#58,
  PRD #42): pure `human_decision(pr) -> Decision|None`, `approval_for(comments, head)`,
  `approval_plan`, `reopen_plan`, `arbitration_prompt`, plus live
  `arbitrate(story, pr, config, root)` and the `--arbitrate-review STORY [CONFIG] [ROOT]
  [--pr PATH]` CLI. Judgement half: the drift-guarded `prompts/arbitration.v1.md`.
  GOTCHAS: (1) Ralph's own reviews are filtered out **by the durable review marker**, not
  by author or event — Ralph posts with the operator's own credential, so "who wrote it"
  cannot tell them apart. Only `APPROVED`/`CHANGES_REQUESTED` decide anything;
  `COMMENTED` is deliberately inert (an AC). (2) An approval is bound to the commit it
  approved (`approval_for(comments, head)`); treating it as blanket permission would let
  anything pushed afterwards merge behind one click. (3) One native review is acted on
  **exactly once**, recorded on the Story by its review id
  (`ralph_review.arbitration_record`/`arbitrated`) — GitHub has no "handled" flag, so the
  fact lives where every other loop fact does. The record is written **last** in the
  Request-changes path, so a launch that never happened is retried rather than marked
  answered; the label move is written **first**, so a crash leaves the Story In Review
  rather than stranded in `state:blocked` with work on the branch. (4) `next_step` reads
  the decision **before** everything else and returns `ARBITRATE`; a recorded approval of
  the current head returns the new `SETTLED`, which ends the wait at exit 0 — open model
  findings never reopen a gate a human released. (5) A blocked Story is never *selected*,
  so the tick could not otherwise notice a human decision on one: `ralph_select.
  blocked_stories` + `--blocked-stories` feed `arbitration_pass`, which runs at the **end**
  of the tick beside the Feature completion pass. Ordering there is load-bearing for the
  queue-driven tick harnesses — every pass consuming a `gh issue list` shifts the mock
  backlog queue, which is why a new pass goes last. A human deciding while the Story is
  still In Review needs none of it: the bounded wait sees it on the next poll.
- `lib/ralph_review_complete.py` closes the loop (#59, PRD #42): pure
  `gate_for(pr, comments) -> Gate` and `completion_plan(...) -> Plan`, the live
  `complete(story, pr, config, root)`, and the `--complete-story STORY [CONFIG] [ROOT]
  [--pr PATH] [--prd PATH]` CLI. `next_step` returns the new `COMPLETE` when the gate
  holds, and `--await-review` exits **16** so the tick completes the Story and carries
  straight on to the next one. GOTCHAS: (1) the gate has **two independent halves** read
  off the *current head's* `statusCheckRollup`: CI is every entry except
  `ralph/model-review`, which is not CI and is judged separately. `_verdict` handles both
  rollup shapes — a CheckRun's `status`+`conclusion` and a StatusContext's `state` — so
  no caller has to know which produced an entry. Pending is not green: it merges only
  when both halves hold, which is why a satisfied review with CI still running is `WAIT`.
  (2) The review half is satisfied by the context reading success (an approving model
  review writes it; a human's Approve writes over it) *or* by a recorded human approval
  of that exact commit — a status write that never landed must not veto an authoritative
  human decision. (3) HIL and AFK take the same gate and diverge only here: a HIL Story
  is **never** merged and never closed, it records a bench anchor at the exact head and
  parks at `state:awaiting-bench`. Model review never replaces physical verification.
  (4) An AFK **Feature** Story closes as Passing *without* merging and leaves its marked
  pull request open — its siblings share it, and a Feature's code integrates only when
  the Feature merges (ADR-0006). Only an Orphan Story merges, and `branching.afk_merge`
  (default `squash`) is honoured: squash is what leaves base one clean commit while the
  pull request keeps every round, fix and dispute. (5) `complete` fetches the PRD itself
  when the Story has a `Parent:`, so no caller has to know to do it first. (6) This is
  the review-gated successor to `--complete-afk`/`--complete-hil`, which remain as the
  pre-review paths; do not add review logic to those.
- Review **records** (#55) live in `lib/ralph_review.py`: `result_record`/`latest_result`
  and `response_record`/`latest_response` (marker + fenced JSON, keyed by head). Written to
  the Story issue when a review publishes (`render_plan(..., story_number=)`) and to the
  pull request when a response posts; `next_step` reads them to tell an unanswered
  changes-requested head from a settled one. They exist because the response round runs in
  a *later process* than the review: recovering findings by parsing back rendered Markdown
  would make loop state depend on prose formatting.
- `lib/ralph_review_wait.py` is the bounded in-tick wait (#54, PRD #42): pure
  `WaitPolicy` (`from_config`, `expired`, `sleep_for`), `next_step(pr)` →
  `REVIEW`/`WAIT`/`GONE`, and `await_review(policy, fetch, act, sleep, now)` returning a
  `WaitResult`; the CLI is `--await-review STORY [CONFIG] [ROOT]` (0 nothing left to wait
  on, **14** window closed → the caller owes a Handoff, 1 a step failed). Config is the new
  optional `review:` section — `wait_minutes` (default 60) and `poll_seconds` (default 30),
  both **numbers** so a test can ask for a sub-second window without an env back door.
  GOTCHAS: (1) the window expiry Handoff is **comment-only**
  (`handoff_plan(..., include_wip=False)`, `ralph --checkpoint ... --comment-only`). The
  normal checkpoint's `git commit --allow-empty` + push would move the pull-request head
  the reviewer's findings are bound to — throwing away the very review the tick just waited
  for and making the next tick re-review (and re-spend) at a new head. (2) A step that
  fails ends the wait immediately rather than being retried each poll: a retry loop over a
  60-minute window is a launch storm. (3) A gh blip during `fetch` returns the
  *last known* pull request, so an outage reads as "nothing new" instead of "the PR is
  gone" — which would otherwise look like a resolved negotiation. Before the first
  successful read there is nothing to wait on, so it ends the wait. (4) `sleep_for` clips
  the final sleep to what is left of the window; overrunning it would hold the tick's lock
  past the point a Handoff was due. (5) The tick calls `--await-review` (not
  `--review-round`) for an In Review Story and always ends the tick afterwards, so waiting
  never competes with new work; the Story keeps `state:in-review`, which is what makes the
  next tick resume it (and rediscover its pull request) ahead of any `state:ready` work.
  The tick-level lock guarantee is tested with a `RALPH_LOCK_PROBE` `flock -n` probe fired
  from inside the mock `gh` — it reports "free" when nothing holds the lock, so the
  assertion is not vacuous.
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
