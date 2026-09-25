# Test harnesses — module notes

Notes for the tick harnesses, the scenario harness (`test/scenarios/`), incident capture (`lib/ralph_capture.py`) and the terminology guard. Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

- The tick harness (`test/unit/test_orchestrate.py`) mocks `git ls-remote --exit-code --heads
  origin <branch>` from `TickHarness.set_remote_branches(...)` — unpinned means "origin has
  none", which is what an unstarted Feature looks like, so a Feature Story's first green
  iteration creates its Feature branch. `TheStoryIsTheUnitOfThePullRequest` (#77) drives
  PRD #69's whole topology through `bin/ralph.sh` offline. GOTCHA: the Feature completion
  pass's gating steps shell `make` (from `full.yml`), so a test that expects the pass to
  reach `gh pr create` must call `harness.mock_make()` — otherwise gating fails and the
  blocker path runs instead, which looks like "nothing happened".
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
- `test/scenarios/` is the **scenario harness** (#87, PRD #85): the real `ralph --run` Tick,
  repeated up to a budget, against a world that keeps state. `harness.World` builds a real git
  checkout with a local **bare remote**, puts `fakes/gh` (stateful GitHub, one JSON state file)
  and `fakes/agent` (scripted provider, installed as both `claude` and `codex`) on PATH, and
  `run_scenario` ticks until every Story in `expect` reaches its terminal state. A scenario is
  data in `worlds/*.json` (config, starting issues, CI rollup, profile per role, Tick budget,
  expected states); `test_scenarios.py` generates one test per world. GOTCHAS: (1) the fake
  **fails closed** -- an unemulated subcommand, flag, `--json` field or API route exits 64 and
  is flagged `unemulated` in the call log, and `check_calls` fails the scenario on it, because
  the Loop swallows some gh failures (`2>/dev/null`) on purpose. Extend the fake for a new call;
  never loosen it. (2) `headRefOid` is **never stored**: it is read from the bare remote when
  asked, so a pushed commit moves the PR exactly as on GitHub; a merged PR keeps its merge-time
  head. `pr merge` is plumbing on the remote and refuses (unemulated) a head that does not
  contain its base. (3) Ralph posts reviews as `COMMENT`; the fake refuses APPROVE/
  REQUEST_CHANGES on its own PR as GitHub does. (4) The agent tells its role from the launch
  flags (`--permission-mode plan` / `--sandbox read-only` = review) and records argv + the full
  prompt in the call log; profiles live in `fakes/agent` `PROFILES`. (5) The World strips every
  inherited `RALPH_*`/`GIT_*`/`GH_*` variable and each adapter's `binary_env` -- the same
  tick-harness gotcha as below.
  **Loop invariants** (#88) live in `test/scenarios/invariants.py`: a fixed, named list that
  `run_scenario` evaluates after **every** Tick via `Watch` (which remembers branch heads between
  Ticks). No scenario declares them. Records are read with `ralph_review`'s own parsers over
  every commit object the remote ever received (`cat-file --batch-all-objects`, so a squashed,
  deleted branch's heads still count). The invocation limit is `limits.max_attempts + 2 *
  review.max_rounds`, read from the world's `.ralph.yml`. `test_invariants.py` holds one
  hand-built violating world per invariant -- add one with any new invariant.
  Negotiation profiles (#89): reviewers `cooperative` (approve), `strict-once` (request changes in
  round 1, then approve), `strict` (uphold F-1 every round); implementation `cooperative` (fix
  commit on the local Story branch + `accepted`; Ralph pushes) and `dispute-all` (no change,
  `disputed` with evidence). A world may carry `known_defect: {story, invariant, why}`: it is
  committed red ahead of its fix, the test asserts it fails **on that invariant**, and it fails
  loudly once it passes so the fixing Story must remove the declaration.
  Replays (#91): a world with `github.capture` may `rewind_to` an ISO moment (drops every
  comment/review/thread item created later; the fake's clock moves past it) and set `labels`
  as they were then. `replay_64_*` replays autopilot_controller #64 / PR #94 from a **redacted**
  capture (`ralph --capture --redact`). `Watch` baselines the starting world's records, so a
  capture's production history never counts against the scenario.
  **Prompt contracts** (#92) are `test/scenarios/contracts.py`: required and forbidden phrases
  per phase (iteration, review, response, arbitration), checked as the `prompt-contract`
  invariant against the prompt each scripted agent *received* -- template plus bundle, because
  the PR #94 defect was in the composition. Since #92 `build_context` emits its read-only /
  no-mutation line only for `for_role="review"`, beside the later-round scope directive.
  **Profiles and the matrix** (#93): `fakes/agent` `PROFILES` adds `literal-noop`, `empty-usage`
  (no output, zero usage), `invalid-output` (malformed contract JSON; an iteration that claims
  done without work), `session-limit` (the CLI's own limit line, exit 1, no transcript),
  `commit-no-push` and `amend-history` (git misbehaviours; a reviewer cannot commit, so under
  those it reviews cooperatively). `agents` may name a **phase** (`iteration`/`review`/
  `response`/`arbitration`) as well as a role; the phase wins. A world's `matrix: {slot:
  {profile: {expect, why, known_defect?}}}` is expanded by `harness.variants` into one test per
  pair. `known_defect` names the open issue and either the `invariant` it breaks or a `failure`
  substring. An expected `in-progress`/`in-review` (`invariants.NON_TERMINAL`) is how a pair
  declares "no progress by design" (session limit); the terminal invariant exempts exactly those.
- `lib/ralph_capture.py` is **incident capture** (#90, PRD #85): `ralph --capture STORY [--out
  DIR]` writes `DIR/story-N.json` in the fake gh's state format -- the Story (labels, comments),
  its PRD when it has a `Parent:`, its newest Ralph-managed PR of any state (comments, REST
  reviews, inline threads, rollup split into per-head statuses + CI checks) and a `git` block.
  Pure `to_state(...)`; `capture(number, cwd)` does only reads (view/list/GET API, `git fetch`).
  GOTCHAS: (1) **no git content is captured**, only the first-parent commit chain merge-base..head
  (oids + subjects): a world with `github.capture` rebuilds a synthetic chain of that shape on
  its own base (which carries the scenario `.ralph.yml`) and `harness.translate_oids` rewrites
  every captured id, whole or abbreviated to 7+, to its replay commit -- plain text substitution,
  so the harness still never parses markers. (2) Comment database ids come from the comment
  `url`, review ids from the REST reviews route (`gh --json` has node ids only). (3) REST lists
  are read with `?per_page=100`; the fake accepts only that query string.
- `test/unit/test_terminology.py` is the repo-wide **docs/terminology guard**: CONTEXT.md's
  `## Language` section is the single glossary (parsed from its `**Term**:` headings), and no
  shipped surface (README/CONTEXT/AGENTS, `docs/`, `prompts/`, `skills/`, `bin/`, `lib/`,
  `schema/`) may contradict it. `ralph/` (the build harness) and `test/` are excluded. It judges
  **paragraphs and sentences, not lines**, because markdown claims wrap; that is what makes the
  stale-invariant check work at all (a guard describing its own forbidden phrasing would trip it). Two escape hatches, both deliberate: a never-`ai-utils`
  sentence is fine when it scopes itself to the submodule mount or names the target repository
  (ADR-0001 amendment), and a doc may spell HITL only in a forbidding context (`never HITL`,
  `_Avoid_: HITL`). New Feature vocabulary goes in CONTEXT.md **and** in this guard's term list.
