# Models, agents, usage and memory — module notes

Notes for `lib/ralph_models.py`, model assignment and role alternation, `lib/ralph_agent.py` launch, `lib/ralph_session.py`, `lib/ralph_usage.py`, `lib/ralph_ledger.py`, `lib/ralph_memory.py`. Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

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
- `lib/ralph_usage.py` normalizes **provider-reported token usage** (#62, PRD #42):
  pure `normalize(provider, raw) -> Usage`, `invocation_event(...)`, `emit(event)` and the
  `--normalize-usage PROVIDER [PATH]` CLI. Five neutral `CATEGORIES` (input, cached_input,
  reasoning, output, total), each carrying a value *and* a `REPORTED`/`UNAVAILABLE` status.
  The counts are obtained by asking each provider for its machine-readable transcript and
  unwrapping it in the adapter: `AgentAdapter.unwrap(output) -> (the agent's words, raw
  usage)`, so `Outcome.output` is still prose and nothing downstream knows a provider ever
  spoke JSON. Claude gets `--output-format json` (envelope: `result` + `usage`); Codex gets
  `--json` (JSONL: `item.completed`/`agent_message` texts + `turn.completed`/`usage`).
  GOTCHAS: (1) the two mappings are **deliberately asymmetric** — Claude's `input_tokens`
  *excludes* cache reads and writes, so normalized input sums all three; Codex's *includes*
  the cached part, so it is taken as-is. Erasing that difference is the whole job; leaving
  it would make "input" mean two things in one ledger. (2) `total` is unavailable for both
  providers because neither states one, and deriving it would double-count (reasoning ⊆
  output, cached ⊆ input) — the AC is to record the gap, not fill it. (3) A category is
  reported only when **every** provider key it sums is present and an int: a partial sum is
  the invented number this exists to avoid, and `0` is a *reading*, which is why the status
  rides alongside. (4) `_unwrap` and `emit` swallow every exception on purpose — accounting
  is telemetry and may never fail a run; a provider that changed its transcript format
  passes its raw output through whole and reports nothing. (5) Unwrapping happens **before**
  `classify`, so the session-limit verdict reads the agent's words, not the JSON around
  them. (6) Emission is on **stderr** (`EVENT_MARKER` + one JSON line): stdout is the
  agent's own words, and `bin/ralph.sh` greps it for the done-signal. (7) `conduct` in the
  round and the response stays pure — it *returns* `usage_event` and the live entry point
  emits it — and an invocation that produced nothing publishable is still accounted for,
  because a ledger counting only successes understates exactly the weeks worth
  understanding. `RALPH_RUN_ID` is exported once per tick so one run's events group.
- `lib/ralph_ledger.py` is the **per-Story token ledger and the PR usage footers**
  (#63, PRD #42): pure `ledger_body` / `parse_payload` / `find_ledger` / `comment_id` /
  `usage_footer` / `ledger_plan(story, comments, event) -> LedgerPlan`, plus the live
  best-effort `record(story, event, cwd=, comments=)`. One machine-managed Story comment
  carries a readable table *and* the versioned `ralph-usage-ledger/v1` payload; the first
  invocation creates it (`gh issue comment`), every later one edits that same comment
  (`gh api --method PATCH .../issues/comments/{id}`). GOTCHAS: (1) the id that endpoint
  needs is the **database** id, and `gh issue view --json comments` reports `id` as a
  GraphQL node id — the database id only survives in the comment's `url`, which is where
  `comment_id` reads it. A ledger with no addressable id is *reported*, never duplicated:
  two comments both claiming to be canonical is the worse outcome. (2) `record` is
  best-effort by construction and takes `comments=` from callers that already read them
  (the round and the response both do), so recording a row costs no extra `gh issue view`.
  (3) `ledger_body` trims the **oldest** rows past `MAX_BODY` (60000, GitHub's 65536 minus
  framing) and states the dropped count in both views — a Story can outrun one comment,
  and silently posting fewer rows would make the ledger quietly wrong. (4) The footer goes
  on both agent responses (`review_body(..., usage_event=)` and `response_comment(...,
  usage_event=)`), which is why `conduct`'s injected `publish` now takes the event as a
  second argument in both stages. (5) A provider that reported nothing gets one honest
  sentence, not "unavailable" five times; a reported `0` still prints as `0`. (6) The
  implementation role's row is written by `ralph_agent._cmd_launch`, because the tick
  launches that role directly rather than through a round.
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
  the tool ships no progress.txt. The two tiers are also enforced **at launch**, in
  `ralph_agent.AgentAdapter.memory_env`: a provider CLI's own cross-run memory is a third
  tier — not in git, and not reviewed by the pull request an `AGENTS.md` change goes through
  — so `environment()` forces each adapter's `memory_env` over the ambient environment
  (`ClaudeAdapter`: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, both roles; `CodexAdapter`: empty,
  that CLI has no known equivalent). GOTCHA: the Claude CLI keys its memory directory on the
  **working directory**, so an unattended iteration and the operator's own interactive
  sessions in the target checkout share one store — on 2026-09-18 an iteration wrote a
  "use a $TMPDIR worktree" workaround there and every later iteration followed it, which is
  the tick's checkout assumption broken by a file no review ever saw. Forcing (not
  defaulting) the variable is the point: an operator setting is exactly where such a feature
  is turned on. Adding a provider adapter is therefore a decision about its memory.
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
- `lib/ralph_context.py` is the **context hand-off signal** (ADR-0004): a `--print` agent
  is told to hand off before its context runs out but is never told how full it is. The
  Claude adapter's implementation role therefore launches with `--settings` carrying a
  `PostToolUse` hook (`hook_settings(threshold)`) that runs `ralph_context.py hook N`:
  it reads the session transcript, sums the latest request's input, cache-write,
  cache-read and output tokens, and past `limits.handoff_context_tokens` (schema default
  150000) injects `additionalContext` naming a **Ralph context notice** — which
  `prompts/iterate.v1.md` tells the agent to answer with a Handoff. GOTCHAS: (1) advice,
  never a gate: a hard stop would end the run without a Handoff, which #95 counts as a
  failed Attempt. (2) The hook swallows every error and exits 0 — a transcript it cannot
  read costs the iteration nothing. (3) Review gets no hook: it runs with
  `--no-session-persistence` (no transcript) and has no Handoff to write. Codex has no
  hook seam, so the signal is Claude's only. (4) There is no documented switch to turn
  Claude Code's auto-compaction off; on native-1M models it fires near ~967k, far past
  the threshold, which is what keeps "Ralph never compacts" true in practice.
