# The scenario harness

The scenario harness (PRD #85) runs the real Ralph Loop — `ralph --run`, one
**Tick** after another — against a world that keeps state, and checks a fixed
list of loop invariants after every Tick. It exists because the Loop's worst
failures are not a wrong line a unit test would flag but a failure to **make
progress across Ticks**: a round answered seventeen times, an iteration
relaunched forty-one times. Those only show up when the Loop is run repeatedly
against a GitHub that remembers what was written.

**The rule for Loop fixes: reproduce it as a scenario first.** A Loop bug is
committed as a scenario that goes red on the current code, then fixed, and the
same scenario turns green with the fix. The worked example at the end of this
guide is how autopilot_controller Story #64 / PR #94 was fixed that way.

## What a scenario runs against

| Piece | Where | What it is |
|---|---|---|
| Fake GitHub | `test/scenarios/fakes/gh` | A `gh` on PATH backed by one JSON state file: issues, labels, comments, pull requests, reviews, inline threads and replies, statuses, merges. A write is visible to every later read, in the same Tick or the next. |
| Git | a bare remote in a temp directory | Real `git`: pushes, ancestry and branch creation behave as in production. A pull request's `headRefOid` is read from the remote when asked, so a push moves it exactly as on GitHub. |
| Scripted agents | `test/scenarios/fakes/agent` | One executable on PATH as `claude` and as `codex`. It answers in the provider's own transcript and usage shape, behaves as the scenario's profile says, and records the argv and the full prompt it was given. |
| Invariants | `test/scenarios/invariants.py` | Checked after **every** Tick of **every** scenario. |
| Prompt contracts | `test/scenarios/contracts.py` | Required and forbidden instructions per phase, checked against the prompts the agents actually received. |

The fake **fails closed**: any `gh` subcommand, flag, `--json` field or API
route it does not emulate exits non-zero with `unemulated gh call: …`, and the
harness fails the scenario on it even when the Loop swallowed the error. When
the Loop starts using new GitHub surface, extend the fake — do not loosen it.

Run the tier on its own with

```sh
python3 -m unittest discover -s test/scenarios -t test/scenarios
```

or as part of the gate, `test/run.sh`, which runs every tier and refuses to
report green if one could not run.

## Writing a scenario

A scenario is data: one JSON file in `test/scenarios/worlds/`. Adding a file
adds a test; no harness code is needed.

```json
{
  "description": "One AFK Orphan Story goes from state:ready to Passing.",
  "config": { "version": 1, "gating": [{"name": "test", "run": "true"}],
              "notify": {"github": "maintainer"},
              "review": {"wait_minutes": 0.1, "poll_seconds": 0.05},
              "models": { "...": "a catalog, as in .ralph.yml" } },
  "github": {
    "ci": [{"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"}],
    "issues": [{"number": 1, "title": "Add the greeting",
                "labels": ["type:afk", "prio:1", "state:ready"],
                "body": "## Acceptance Criteria\n- [ ] the greeting exists\n\nParent: None\nDepends on: None\n"}]
  },
  "agents": {"implementation": "cooperative", "review": "cooperative"},
  "ticks": 3,
  "expect": {"1": "passing"}
}
```

- `config` becomes the world's committed `.ralph.yml`. Keep the review window
  small (`wait_minutes: 0.1`) so a scenario finishes in seconds.
- `github.issues` is the starting backlog; the canonical labels are seeded for
  you. `github.ci` is the CI rollup every head reports.
- `agents` names a behaviour profile per **role** (`implementation`, `review`)
  or per **phase** (`iteration`, `review`, `response`, `arbitration`); a phase
  entry wins, so a Story can iterate cooperatively and then answer a review
  badly.
- `ticks` is the budget; the runner stops early once every Story in `expect`
  has reached its state.
- `expect` maps a Story to `passing`, `blocked`, `needs-human` or
  `awaiting-bench` — or, only when that is the point of the scenario, to a
  state that is not finished (`ready`, `in-progress`, `in-review`), which
  exempts that Story from the terminal invariant.
- `tick_exit` is the exit code a Tick is expected to end with (default 0), for
  a scenario whose point is that the Tick refuses.
- `mount_ai_utils: committed | drifted` mounts a copy of the ai-utils under
  test inside the world as a submodule, for the Superproject pointer check.

### The profile matrix

A world's `matrix` runs it again with one profile swapped in, once per pair,
each with its own expected state:

```json
"matrix": {
  "response": {
    "literal-noop": {"expect": {"1": "needs-human"},
                     "why": "No commit and every Finding unresolved is not an answer."}
  }
}
```

## Agent profiles

Each profile is a behaviour per phase in `PROFILES` in `fakes/agent`:

| Profile | Behaviour |
|---|---|
| `cooperative` | Commits and pushes the Story's work and signals done; reviews approve; answers a review by appending a fix commit and accepting every Finding. |
| `strict-once` / `strict` | Review only: request changes in round one then approve / request changes every round. The Finding anchors on the diff the reviewer was actually given. |
| `dispute-all` | Answers a review by changing nothing and disputing every Finding, with evidence. |
| `literal-noop` | Follows the most restrictive instruction it was given and changes nothing: no commit, every Finding `unresolved`, a review with no findings. |
| `empty-usage` | Exits cleanly with no output and zero tokens. |
| `invalid-output` | Malformed contract JSON; an iteration that signals done without doing the work. |
| `session-limit` | The provider's own session-limit line, exit 1, no transcript. |
| `commit-no-push` / `amend-history` | Git misbehaviour: commits without pushing / rewrites the branch. A Review Agent cannot commit, so under these it reviews cooperatively. |

To add a profile, write a function per phase that takes the run's context
(`prompt`, `model`, `story`, `head`, `round`, `provider`) and returns the
agent's words, `(words, usage)`, or `Raw(text, rc)` for output that is not a
transcript at all, and register it in `PROFILES`. A responder self-reports a
wrong model on purpose: what an agent says it is, is not evidence of what ran.

## Loop invariants

After every Tick the runner evaluates each of these against the fake's state,
the call log and the remote. A violation stops the scenario and reports the
Tick, the invariant, the records involved and the last calls.

| Invariant | Holds when |
|---|---|
| `one-result-per-round` | At most one Review Agent result per (head, Negotiation Round). |
| `one-response-per-round` | At most one Implementation Agent response per (head, Negotiation Round), wherever it was recorded. |
| `invocation-limit` | Model invocations per Story stay within `limits.max_attempts + 2 × review.max_rounds`, read from the world's config. |
| `branch-only-grows` | Every branch head descends from the head it had after the previous Tick. |
| `empty-runs-never-count` | A run with no output or zero usage never backs a recorded result, a recorded response or a promotion into review. |
| `prompt-contract` | Every prompt an agent received carries its phase's required instructions and none of its forbidden ones. |
| `terminal-within-budget` | By the last Tick every Story is Passing, `state:blocked`, `needs-human` or `state:awaiting-bench`. |

Records are always read with the Loop's own parsers (`ralph_review`), never by
re-implementing its markers in the harness. Records a world *starts* with — a
capture's production history — are the baseline and never count against the
scenario.

To add an invariant, write a function from an `Observation` to a list of
`Violation`s, add it to `INVARIANTS`, and add a **hand-built world that
violates it** to `test/scenarios/test_invariants.py`. An invariant that has
never been seen red proves nothing.

## Capturing a production incident

```sh
cd <target repository>
ralph --capture 64 --out /tmp/capture --redact
```

`ralph --capture STORY` reads, through the real `gh` and with reads only, the
Story (labels, comments), its PRD, its newest Ralph-managed Story Pull Request
(comments, reviews, inline threads, check rollup), and the pull request's
commit chain, and writes them in the fake's state format. No git content is
captured: a world rebuilds a synthetic commit chain of the same shape and
translates every captured commit id to its replay counterpart.

`--redact` makes a capture of a private repository safe to commit to a public
one. It is a whitelist: every marker, every record's structural fields, the
Finding headings and the generated header lines survive; all prose becomes
`[redacted]`, titles become `Story N` / `PRD N`, branches are renamed to what the
Loop derives from those titles, and file paths are mapped consistently. Read
the output before committing it.

A world loads a capture with

```json
"github": {
  "capture": "captures/autopilot-controller-64.json",
  "rewind_to": "2026-09-23T08:18:36Z",
  "labels": {"64": ["prio:5", "type:afk", "state:in-review",
                    "model:impl:gpt-5.6-sol", "model:review:claude-opus-5"]}
}
```

A capture is the Story as it is *now*; `rewind_to` replays it from the moment
the incident started, dropping every comment, review, thread item and status
recorded later, dropping a pull request opened later and reopening one merged
later. `labels` restores the labels as they were then.

## Committing red: `known_defect`

A scenario — or one matrix pair — that fails on the current code is committed
with the open issue that will fix it:

```json
"known_defect": {"story": "#91", "invariant": "one-response-per-round",
                 "why": "The response record is posted to the pull request, but needs_review() counts answers on the Story."}
```

The test then **requires** it to fail on that invariant (or on a `failure`
substring, for a Tick that fails outright), and fails loudly the moment it
passes: the Story that fixes it must remove the declaration in the same
change.

## Worked example: autopilot_controller #64 / PR #94

In production, after one round-one review requested changes, the
Implementation Agent answered the same head seventeen times.

1. **Capture.** `ralph --capture 64 --redact` in the target repository; the
   file went to `worlds/captures/autopilot-controller-64.json`.
2. **Replay, red.** `replay_64_dispute.json` loads it, rewinds to the moment the
   round-one result was recorded, and runs a responder that changes nothing and
   disputes every Finding — as codex effectively did. On the unfixed code:

   ```
   tick 1: invariant one-response-per-round violated: #64 head b709bcd round 1 answered 7 times; limit 1
   ```

   It was committed with `known_defect` naming #91.
3. **Root cause from the trace.** The call log showed each answer posted with
   `gh pr comment`, while every reader of the negotiation state reads the
   Story's comments. The round never read as answered, so each poll launched
   the responder again. A cooperative responder had masked it for months:
   its fix commit made a new head that was simply unreviewed.
4. **Fix, green.** The record moved to the Story (#91). The replay then ended
   in a bounded number of rounds, escalated, with every invariant holding, and
   its `known_defect` came off in the same commit.
5. **What else it caught.** Adding the `prompt-contract` invariant turned every
   negotiation scenario red on the second half of that incident: the reviewer's
   read-only line reached the responder (#92).
