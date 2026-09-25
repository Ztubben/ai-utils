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
- `test/run.sh` — the green gate. `test/unit/` = Python `unittest` (fixtures under `test/fixtures/`); `test/bats/` = bats orchestration (bats is **required**: `run.sh` fails fast without it, #86); `test/scenarios/` = the multi-Tick scenario tier (PRD #85), run when present -- read `docs/scenario-harness.md` before writing a scenario, profile or invariant, and reproduce a Loop bug as a red scenario before fixing it. `run.sh` ends by naming the tiers that ran.

## Conventions / gotchas
- Publication must name both refs explicitly: `refs/heads/<story>:refs/heads/<story>`.
  The orchestration checkout may remain on a parked branch while the agent works
  in another worktree. Never infer a Story's published commit from that checkout's
  `HEAD` or change its upstream with `push -u`. A missing Story ref must fail.
  The same holds for review rounds: read and push the pull request's own
  `headRefName` ref, never the checkout's `HEAD`.
- Completion never deletes the *local* story branch (`gh pr merge --delete-branch`
  fails after the merge when that branch is checked out in a worktree, stranding
  the Story merged but open). The remote branch is deleted by name as best-effort
  cleanup after the Story is closed. A Story In Review whose pull request is
  already merged is finished (`FINISH`: close, merge nothing), never reported gone.
- No `pytest`; unit tests use stdlib `unittest`, run via `test/run.sh`. `bats` must be installed.
- Python logic returns a result object (`ok`, `errors`, resolved data) rather than
  exiting; only the CLI wrapper prints and sets exit codes. Keeps logic unit-testable.
- Error strings name the offending field path (e.g. `branching/afk_merge: ...`) so
  `--check-config` failures are actionable.
- Config validation is JSON-schema Draft-7 with `additionalProperties: false`, which is
  how the mandated label scheme stays non-overridable (unknown keys like `labels:` fail).
- Schema `default`s are applied by `lib/ralph_config.py` after validation (jsonschema
  does not fill defaults itself).
- **Test harness gotcha**: the tick harnesses (`test/unit/test_orchestrate.py`,
  `test/unit/test_freshness.py`, `test/bats/orchestration.bats`) inherit the ambient
  environment. A Ralph iteration exports the provider binary overrides (`RALPH_CLAUDE` /
  `RALPH_CODEX`), which beat the fakes on `PATH` (`AgentAdapter.binary()` prefers
  `binary_env`) and make the suite launch a **real** agent — a hang, not a failure. Each
  harness therefore strips every adapter's `binary_env` from the child env; keep that when
  adding a harness that runs `bin/ralph.sh`.

## Module notes — read the file for the code you touch
Detailed per-module notes (seams, invariants, GOTCHAs, the issue each came from) live in
`docs/agents/`, not here: this file is loaded into every session, so it stays an index.
Before changing a module, read its notes file; put a new learning in the file that owns it.
- `docs/agents/stories.md` — story shape, selection (`ralph_select`), `ralph_iterate`
  branch/base topology, the Story as the unit of the pull request (PRD #69).
- `docs/agents/completion.md` — plan → run completion (`ralph_afk`, `ralph_hil`),
  Handoffs (`ralph_handoff`), failed Attempts and the circuit breaker (`ralph_failure`).
- `docs/agents/review.md` — every `ralph_review*` module: bundle, contract, rendering,
  rounds, responses, disputes, deadlock, human arbitration, completion gate, protected
  control plane, bounded wait, review records (PRD #42).
- `docs/agents/agents.md` — model profiles, assignment and alternation, agent launch and
  the context hand-off signal (`ralph_agent`, `ralph_context`), session limits, token
  usage and the ledger, two-tier memory.
- `docs/agents/tick.md` — `bin/ralph.sh`, the Superproject pointer check, `ralph --init`,
  schedulers, `ai-utils`' own deployment.
- `docs/agents/testing.md` — tick harnesses, the scenario harness and its invariants,
  profiles and replays, incident capture, the terminology guard.
