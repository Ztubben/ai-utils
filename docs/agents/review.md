# Review and negotiation — module notes

Notes for `lib/ralph_review*.py`: the review bundle, contract, rendering, rounds, responses, disputes, deadlock, human arbitration, completion gate, protected control plane, bounded wait and review records (PRD #42). Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

- `lib/ralph_review.py` owns the exact durable pull-request opt-in marker and
  `is_managed_pr`; an unmarked PR is never eligible for automated review. Locally green
  implementation promotion (#49) lives in `lib/ralph_implementation.py`: its pure
  `implementation_green_plan` pushes the Story's own branch, creates a marked PR
  or updates the already-open marked PR **against the Story's resolved base**, and moves
  both AFK and HIL Stories to `state:in-review`. It never merges, closes, or emits
  `state:awaiting-bench`, and refuses `main` and an unmarked existing PR. Since #71
  (PRD #69) each Story owns exactly one pull request: a Feature Story's targets its Feature
  branch, which the plan creates off the base branch when `feature_exists=False` (the CLI
  asks origin, via `remote_branch_exists`, and only for a Story that has a Feature at all).
  GOTCHA: the review bundle needed **no** change for this — its diff is the pull request's
  own `baseRefOid..headRefOid`, so once the base is the Feature branch that range *is* the
  Story's change and a sibling's commits sit behind it. `bin/ralph.sh` calls `--implementation-green` for the
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
  (5) Unsetting those variables is only half of it (#64): `gh` also authenticates from
  `$GH_CONFIG_DIR/hosts.yml`, else `$XDG_CONFIG_HOME/gh`, else `~/.config/gh` — a logged-in
  operator's file, which no environment strip reaches. The review role therefore *sets*
  `GH_CONFIG_DIR` to `ralph_agent.credential_free_config_dir()`, an empty per-process
  directory (`GH_CONFIG_DIR` overrides the whole lookup, so one variable closes all three
  paths). The implementation role keeps its credential — it pushes.
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
  (4) Since #73 (PRD #69) completion is uniform: every Story merges **its own** pull
  request into **its own** base per `branching.afk_merge` (default `squash` — one clean
  commit on the base while the pull request keeps every round, fix and dispute) and deletes
  the branch. An AFK Story closes as Passing there; a HIL **Feature** Story merges the same
  way and *then* parks at `state:awaiting-bench`, so its successors build on its code. The
  one Story that is still never merged before the bench is a HIL **Orphan** Story: nothing
  stands between it and the base branch. Its anchor names the reviewed commit plus
  `git fetch origin refs/pull/N/head`, because the branch is gone after the merge. (5) `complete` fetches the PRD itself
  when the Story has a `Parent:`, so no caller has to know to do it first. (6) This is
  the review-gated successor to `--complete-afk`/`--complete-hil`, which remain as the
  pre-review paths; do not add review logic to those.
- The **protected control plane** (#60, PRD #42) is config plus one extra condition on
  the completion gate, not a module of its own. `control_plane.protected` is a list of
  repository-relative path patterns in the schema; `ralph_config._control_plane_errors`
  rejects an absolute or `..`-escaping pattern, because such a pattern can never match a
  repository-relative changed path and would therefore silently protect *nothing* —
  the worst failure mode for a rule whose whole job is to stop the mechanism approving
  changes to itself. `ralph_review_complete.protected_paths(changed, patterns)` matches
  (a bare directory protects everything under it; otherwise fnmatch, where `*` crosses
  `/` on purpose so `prompts/**` reads as expected), and `gate_for(..., protected=)`
  then requires the review half to be satisfied **by the human**, never by the check.
  GOTCHAS: (1) `Gate.held_for_human` is true only when everything else is already
  ready — asking a person to approve work CI has not finished checking spends their
  attention on a head that may not survive. (2) The notice (`hold_plan` → a pull-request
  comment naming the paths, an `--add-reviewer`, and a per-head record) is posted **once
  per head**, guarded by `ralph_review.control_plane_held`; after that the poll simply
  returns `WAIT`. Nothing is blocked and nothing is closed: the negotiation is over, not
  broken. (3) `protected_for` shells git **only when patterns are declared**, so a
  repository that protects nothing pays nothing on every poll of every window. (4) The
  wait's `read_protected` **fails closed**: if the reviewed diff cannot be read it
  reports an unknown protected path rather than an empty list, because failing open
  would merge a control-plane change on a model review alone. (5) `ai-utils`'s own
  `.ralph.yml` declares its review machinery protected — it is its own target repository
  (ADR-0001 amendment), and dogfooding the rule is the point of it.
- **Where the response record lives** (#91): on the **Story**, never the pull request.
  `respond_to_review`'s publish posts thread replies, then the prose-only `response_comment` on
  the PR, then `record_command` (the `ralph-review-response:v1` record) on the Story, last --
  because every reader (`needs_review`, `rounds_answered`, deadlock, history) reads Story
  comments. Posted on the PR, a disputed round never read as answered and the responder was
  relaunched every poll (autopilot_controller PR #94: 17 answers to one head). A fix commit used
  to mask it: the new head was simply unreviewed.
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
- **Infrastructure failures never spend a round** (#61, PRD #42) is two small changes,
  not a module. (1) `ralph_review_wait.act` now returns `(ok, errors, retryable)` —
  built by the new `step_outcome(rc, what)` — and `await_review` rides a *retryable*
  failure out inside the window (counting `WaitResult.retries`) instead of ending on
  it. `RETRYABLE_EXITS` is exactly two codes: `ralph_agent.EXIT_INFRASTRUCTURE_FAILURE`
  (12) and the new `ralph_review_round.EXIT_INVALID_OUTPUT` (**17**), which INVALID_OUTPUT
  needed because it used to share the flat refusal code 2 — and a refusal (bad config,
  unmarked PR, refused role resolution) would refuse identically on the next poll, so
  retrying it is a launch storm. `ralph_review_respond` maps its own INVALID_OUTPUT to
  the same 17, but keeps NOT_APPEND_ONLY at 2: an amend or force-push is the *model*
  breaking the protocol, not the provider failing. A session limit (10) is not retryable
  either — the budget a retry would spend is the thing that ran out. No round is spent
  by any of them because a round is a **published review stamp**, and nothing published.
  (2) `ralph_models.reassign_plan` + `ralph --reassign-model STORY ROLE PROFILE [CONFIG]
  --reason TEXT [--allow-same-model]` is the human-only reassignment: label create →
  `issue edit --add-label/--remove-label` → `issue comment` carrying
  `REASSIGNMENT_MARKER` + the payload. GOTCHAS: (a) the record is written **last** — a
  crash then leaves the labels right and the record missing, which beats a record of a
  swap that never happened. (b) `--reason` is required: an audit record without one
  records only that someone did it, which the labels already say. (c) It refuses a role
  with no assignment to replace (that is `assign_plan`'s job, and it only ever *fills*),
  a replacement equal to the identity already recorded, and a swap collapsing both roles
  onto one identity. (d) A test drift-guards that `bin/ralph.sh` never names
  `--reassign-model`: "human-only" is enforced by nothing else. Covered by
  `test/unit/test_infrastructure.py`.
- **A no-op answer is refused and escalated** (#94): `ralph_review_respond.no_answer_errors` --
  head unchanged **and every** disposition `unresolved` -> `NO_ANSWER` (exit `EXIT_NO_ANSWER`, 18,
  not retryable). The live path runs `escalation_plan` (Story notice first, then `needs-human`,
  which halts the loop via selection). A dispute with no commit stays an answer. `conduct`
  overwrites `answer["model"]` with the **launched** `outcome.model` before validation, so the
  Story record, the PR comment and the ledger name one model (PR #94 recorded the agent's
  self-reported `gpt-5`). The scripted responder self-reports a wrong model on purpose.
