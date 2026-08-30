# ai-utils — the Ralph Loop

Reusable, project-agnostic tooling shared across embedded projects as a **git
submodule**. Its centerpiece is the **Ralph Loop**: an autonomous coding-agent
loop that works through a GitHub-issue backlog, implementing stories test-first
until the local quality gate is green.

ai-utils carries no consuming project's source, issues, or CI. You mount it inside
a host project (the *superproject*) and drive it from there; Ralph then modifies
only that **target repository** and never the mounted ai-utils checkout. When
ai-utils is itself the checkout root it is its own target repository and may work
its own backlog (ADR-0001 amendment). Either way, Ralph **never touches `main`**.

---

## Table of contents

- [What is the Ralph Loop?](#what-is-the-ralph-loop)
- [Core concepts](#core-concepts)
- [How it works (one tick)](#how-it-works-one-tick)
- [Requirements](#requirements)
- [Getting started](#getting-started)
- [Installing the scheduler](#installing-the-scheduler)
- [Configuration (`.ralph.yml`)](#configuration-ralphyml)
- [Authoring the backlog](#authoring-the-backlog)
- [The `ralph` CLI](#the-ralph-cli)
- [Failure handling](#failure-handling)
- [Memory & learnings](#memory--learnings)
- [Self-hosting: ai-utils' own deployment](#self-hosting-ai-utils-own-deployment)
- [Project layout](#project-layout)
- [Running the tests](#running-the-tests)
- [Design decisions](#design-decisions)

---

## What is the Ralph Loop?

Give Ralph a backlog of well-formed GitHub issues and a config that says how to
build and test your project, and it will:

1. Pick the highest-priority ready story with no open blockers.
2. Create a branch and implement the story **test-first** (red → green).
3. Run your configured quality checks locally (a cheap mirror of CI).
4. When implementation is green, open or update a marked pull request and move
   the story into independent model review. Final AFK merge or HIL bench handoff
   happens only after review succeeds.
5. Move on to the next story, and keep going until the backlog is empty, a
   session budget runs out, or something needs a human.

Ralph is designed for embedded work where some acceptance criteria can only be
confirmed on real hardware — so it distinguishes work CI can prove from work a
human must verify on the bench.

## Core concepts

| Term | Meaning |
| --- | --- |
| **Superproject** | The host project that mounts ai-utils as a submodule. Ralph runs from its root and only modifies it. |
| **Story** | One unit of backlog work = one GitHub Issue, carrying `state:` / `type:` / `prio:` labels. |
| **AFK story** (`type:afk`) | *Away-From-Keyboard.* Fully verifiable without a physical bench (logic, parsing, refactors, build config), but still passes independent model review before final merge. |
| **HIL story** (`type:hil`) | *Human-In-the-Loop.* Needs a human to confirm real behavior on the physical bench (GPIO, timing, sensors). Green implementation and model review are necessary but **not** sufficient. |
| **Blocker** | A story needing a human *design decision before coding.* Kept out of `state:ready` (labelled `ready-for-human`) so Ralph never picks it up. |
| **Tick** | One scheduled run of the loop (every ~5 hours). Resumes any in-progress story first, then works as many ready stories as the session budget allows. Only one tick per superproject runs at a time (guarded by a `flock`). |
| **Iteration** | A single fresh-context agent process inside a tick. Ralph **never compacts context** — when it fills, the iteration writes a *Handoff* and the next iteration resumes with clean context. |
| **Handoff** | The checkpoint an iteration leaves so a story can resume: a summary as an issue comment + WIP commits on the story branch. |
| **Story pull request** | The one Ralph-managed pull request a story owns, from its own branch into its own base. An Orphan story targets the base branch; a story belonging to a Feature targets that Feature's integration branch, which merges into the base branch as one piece when the Feature is complete ([ADR-0006](docs/adr/0006-per-feature-integration-branches.md)). A Feature's stories never share one, so the reviewed diff and the round budget are each story's own. |
| **Gating steps** | The build/test/lint checks Ralph must pass before a story counts. You declare them in `.ralph.yml`. |
| **Learnings** | Durable, reusable knowledge Ralph records in nested `AGENTS.md` files in the superproject (there is no `progress.txt`). |

### The label scheme (source of truth)

The backlog lives entirely in GitHub Issue **labels + body conventions** — this
is what Ralph reads, not a Projects board. The scheme is **mandated and not
configurable**:

- **State** (exactly one): `state:ready` → `state:in-progress` →
  `state:in-review` → `state:awaiting-bench` → *closed* (= Done). AFK
  Stories close directly from In Review; HIL Stories pass through Awaiting
  Bench Verification.
- **Type** (exactly one): `type:afk` or `type:hil`.
- **Priority** (optional, at most one): `prio:N`, lower = higher priority. A story with no `prio:N` sorts as lowest priority; ties (and prio-less stories) break by lowest issue number (FIFO). Add `prio:N` only to jump the queue.
- **Dependencies**: a `Depends on: #12, #34` line in the body. A story is
  ineligible until every dependency is *Passing* (closed) — for a HIL dependency
  that means bench-verified.

## How it works (one tick)

```
scheduler (every ~5h)
        │
        ▼
   bin/ralph.sh  ──► flock (one tick at a time)
        │
        ▼
   validate .ralph.yml  (fails loud if invalid)
        │
        ▼
   ralph --dry-run  ──► next action: resume #N | start #N | no-work | halt
        │
        ▼
   fresh-context Implementation Agent  (ralph --launch-agent, prompts/iterate.v1.md)
        │   implements the story test-first, runs gating
        ├── green (AFK/HIL) ─► ralph --implementation-green
        │                      (marked PR, → state:in-review)
        │                          │
        │                          ▼
        │                      ralph --await-review  (the tick waits here, holding
        │                      its lock: polls durable state, backing off, and runs
        │                      ralph --review-round when the head has no review —
        │                      fresh Review Agent, read-only, no GitHub credential,
        │                      concurrent with CI, prompts/review.v1.md)
        │                          │
        │                          ├── changes requested ─► ralph --respond-review
        │                          │   (fresh implementation round: append-only fix
        │                          │   commits, or a *dispute* backed by evidence that
        │                          │   changes no code — thread replies plus one
        │                          │   machine-readable response, prompts/respond.v1.md)
        │                          │       │
        │                          │       └─► a fresh Review Agent judges the answered
        │                          │           head again — the new one after a fix, the
        │                          │           same one after a dispute — and withdraws
        │                          │           or upholds each finding by identifier.
        │                          │           Later rounds adjudicate only what is open;
        │                          │           a new blocker may be raised late only as a
        │                          │           defect or safety regression, and never
        │                          │           extends the round limit.
        │                          │
        │                          ├── touches control_plane.protected ─►
        │                          │   notice on the PR saying why, native
        │                          │   review requested; nothing completes
        │                          │   until a person Approves (either type)
        │                          │
        │                          ├── CI green + review gate satisfied ─►
        │                          │   ralph --complete-story
        │                          │     AFK  ─► squash-merge into base, close
        │                          │             as Passing (a Feature story
        │                          │             closes without merging)
        │                          │     HIL  ─► bench anchor + state:awaiting-
        │                          │             bench, still open, never merged
        │                          │
        │                          ├── human Approve ─► gate released over any
        │                          │   open findings, escalation cleared, the
        │                          │   override recorded on the Story
        │                          ├── human Request changes ─► back to
        │                          │   state:in-review, implementation model
        │                          │   launched with the human's own words
        │                          │   (prompts/arbitration.v1.md)
        │                          │
        │                          ├── rounds spent, still disagreeing ─►
        │                          │   ralph --escalate-review (both sides'
        │                          │   arguments on the PR, native review
        │                          │   requested from notify.github, this Story
        │                          │   → state:blocked). The tick works on;
        │                          │   the circuit breaker still decides whether
        │                          │   *this many* blocked Stories halt the loop.
        │                          │
        │                          └── window closes ─► comment-only Handoff,
        │                              tick ends; next tick resumes this Story first
        ├── context full ──► ralph --checkpoint     (Handoff, resume next iteration)
        └── failed       ──► ralph --record-attempt (block after max_attempts)
        │
        ▼
   loop to next eligible story until no-work / halt / session budget spent
```

## Requirements

- **Bash** and **Python 3** (stdlib + [`PyYAML`](https://pypi.org/project/PyYAML/) + [`jsonschema`](https://pypi.org/project/jsonschema/)).
- The **[`gh`](https://cli.github.com/) GitHub CLI**, authenticated for the superproject (Ralph reads the backlog and opens PRs through it).
- A **provider CLI on `PATH` for every adapter your model catalog uses** — Ralph launches it to do the actual implementation: [Claude Code](https://claude.com/claude-code) (`claude`) for the `claude` adapter, [Codex CLI](https://developers.openai.com/codex/cli) (`codex`) for the `codex` adapter. Override a binary's path with `RALPH_CLAUDE` / `RALPH_CODEX`.
- Whatever your gating steps need (e.g. `make`, a toolchain, a test runner).

## Getting started

1. **Add ai-utils as a submodule** of your project:

   ```sh
   git submodule add <ai-utils-repo-url> ai-utils
   ```

2. **Create your config.** Copy the documented sample to the superproject root
   and edit it:

   ```sh
   cp ai-utils/.ralph.yml.sample .ralph.yml
   $EDITOR .ralph.yml
   ```

3. **Validate the config:**

   ```sh
   ai-utils/bin/ralph --check-config
   ```

4. **Initialize the repo** — create the canonical labels and the base branch
   (idempotent; safe to re-run). Do this once per superproject, before authoring
   stories, or `gh issue edit --add-label state:…` will fail with `not found`:

   ```sh
   ai-utils/bin/ralph --init
   ```

5. **Author some stories** as GitHub issues in the canonical shape (see
   [Authoring the backlog](#authoring-the-backlog)), and lint them:

   ```sh
   gh issue view 42 --json number,title,labels,body | ai-utils/bin/ralph --lint-story -
   ```

6. **Dry-run the selector** to see what Ralph would pick up next — this changes
   nothing:

   ```sh
   ai-utils/bin/ralph --dry-run
   ```

7. **Run a tick** manually to try the full loop, or wire `ai-utils/bin/ralph.sh`
   into a scheduler (e.g. cron every 5 hours) for unattended operation:

   ```sh
   ai-utils/bin/ralph.sh
   ```

> Tip: add `ai-utils/bin` to your `PATH` so you can just type `ralph …`.

## Installing the scheduler

Unattended operation is just a **tick every 5 hours**. ai-utils ships sample
scheduler units under [`scheduler/`](scheduler/) — pick **one**:

- `scheduler/ralph.service` + `scheduler/ralph.timer` — a systemd timer, or
- `scheduler/ralph.cron` — a single crontab line.

Both run `ai-utils/bin/ralph.sh` (the tick) from your superproject root. The tick
is flock-guarded, so a scheduled run that overlaps a still-running one is a
harmless no-op.

### Auth prerequisites (do this first)

A tick runs unattended, so both tools must already be authenticated **as the user
the schedule runs as** (a systemd *user* unit and a personal crontab both run as
you and reuse your `~/.config` credentials):

```sh
gh auth login            # GitHub CLI — Ralph reads the backlog + opens PRs
claude                   # sign in once so `claude` on PATH is authenticated
```

Also make sure `git`, `python3` (+ `PyYAML`, `jsonschema`), and whatever your
gating steps need are on the `PATH` the scheduler uses (cron in particular starts
with a minimal environment — see the `PATH=` line in `scheduler/ralph.cron`).

### Option A — systemd timer (recommended)

```sh
mkdir -p ~/.config/systemd/user
cp ai-utils/scheduler/ralph.service ~/.config/systemd/user/
cp ai-utils/scheduler/ralph.timer   ~/.config/systemd/user/
# edit the two paths in ralph.service to point at your superproject, then:
systemctl --user daemon-reload
systemctl --user enable --now ralph.timer
loginctl enable-linger "$USER"    # let the timer run while you're logged out
systemctl --user list-timers ralph.timer   # confirm the next 5-hour fire
```

### Option B — cron

```sh
# edit the superproject path in the entry first:
crontab -e
# then append the line from ai-utils/scheduler/ralph.cron:
#   0 */5 * * *   cd /path/to/your/superproject && ai-utils/bin/ralph.sh >> .ralph.log 2>&1
```

Once installed, Ralph wakes every 5 hours, resumes any active Story
(`state:in-progress` or `state:in-review`), works as many Ready Stories as the
session budget allows, then sleeps until the next
tick. Run `ai-utils/bin/ralph.sh` by hand once first to confirm the config and
auth are good before leaving it unattended.

## Configuration (`.ralph.yml`)

Everything project-specific lives in `.ralph.yml` at the superproject root. It is
validated against the shipped JSON-schema at tick start; an invalid or missing
config **fails loud** rather than defaulting. The label scheme above is *not*
configurable — unknown keys (like `labels:`) are rejected.

```yaml
version: 1

# Ordered quality checks Ralph runs locally before a story counts as green.
# Run in order, fail-fast, low-verbosity.
gating:
  - name: build
    run: make build
  - name: test
    run: make test

# Ralph integrates into `base` and never touches `main`.
branching:
  base: develop                            # default: develop
  branch_pattern: "ralph/{issue}-{slug}"   # every story's own working branch
  feature_pattern: "feature/{issue}-{slug}"  # {issue}/{slug} from the PRD issue
  afk_merge: squash                        # merge | squash | rebase (default: squash)

# Failure-handling limits.
limits:
  max_attempts: 3      # failed Attempts before a story → state:blocked (default: 3)
  circuit_breaker: 2   # blocked stories that halt the loop + tag a human (default: 2)

# Allowlisted Model Profiles + the committed defaults for the two model roles.
models:
  profiles:
    - key: claude-opus         # stable profile key; roles are selected by key
      provider: claude         # provider adapter: claude | codex
      model: claude-opus-5     # exact model identity, persisted on the story
    - key: codex-high
      provider: codex
      model: gpt-5-codex
  defaults:
    implementation: claude-opus   # must name a profile above
    review: codex-high
  alternate: true                 # swap the pair on each newly started story (default: true)

# Model review and negotiation.
review:
  wait_minutes: 60     # bounded negotiation window per tick (default: 60)
  poll_seconds: 30     # first poll; later polls back off, capped, in the window
  max_rounds: 2        # rounds before the disagreement goes to a human (default: 2)

# The protected control plane: the parts of this repository that govern Ralph's
# own review gate. A Story touching one is never completed on a model review
# alone — a person must Approve the pull request first, whatever the type.
control_plane:
  protected:
    - .github/workflows/**
    - .ralph.yml

# Who gets tagged when the circuit breaker trips (needs-human), asked to
# arbitrate a deadlock, or asked to approve a control-plane change.
notify:
  github: your-github-handle   # no leading @
```

`version`, `gating`, and `notify` are required; everything else has sensible
defaults. See `.ralph.yml.sample` for the annotated original.

### Model profiles and the two roles

The catalog is the allowlist: Ralph will only run a model that appears in it. The
committed `defaults` let a scheduled tick resolve an Implementation Agent and an
independent Review Agent with no command-line arguments; an operator overrides
either role by profile key at tick start:

```sh
ralph --resolve-models                                   # committed defaults
ralph --resolve-models --review codex-high               # override one role
ralph --resolve-models --implementation codex-high --review claude-opus
```

`--check-config` rejects an unknown provider adapter, a duplicate profile key, and
a default naming a profile that is not in the catalog, naming the offending field.
Resolution **refuses** a pair whose two roles resolve to the same model identity —
the exact `model` string, whichever adapter runs it — so the independence of
review is never lost by accident. Single-model operation stays available on
purpose, via the explicit `--allow-same-model` acknowledgement.

Both roles are launched through **one adapter interface**, so the orchestration
never names a provider. An adapter resolves its role's Model Profile, launches
that exact model identity in a **fresh process** carrying no inherited session
state (no resume flag; the provider's own session variables are stripped from the
child environment), and classifies what came back as normal completion, session
exhaustion, or infrastructure failure. `ralph --launch-agent` exposes that as a
CLI — prompt on stdin, the agent's output on stdout, the outcome as the exit code
(`0` normal, `10` session exhaustion, `12` infrastructure failure) — which is how
the tick drives an iteration:

```sh
printf '%s' "$prompt" | ralph --launch-agent implementation
```

A target repository that has not declared a `models:` catalog keeps ticking on
the `claude` adapter at that CLI's own default model.

### The assignment is durable, on the story

Resolution decides which two models a story runs; the tick then **records that
decision on the story itself**, as two labels carrying the exact model
identities:

```
model:impl:claude-opus-5      model:review:gpt-5-codex
```

The labels are created on demand the first time an identity is assigned, so the
catalog can grow without re-running `ralph --init`. From then on the story is the
source of truth: a resume, a retry, or an audit reads its roles from the backlog,
not from whatever configuration happens to be current. **Changed defaults and
`--implementation`/`--review` overrides never rewrite an assigned story** — they
only ever decide a role that has no label yet. Same-model independence is
likewise settled once, when the pair is chosen, so a later config change cannot
strand a story that is already in flight.

```sh
ralph --assign-models story.json          # idempotent; a no-op once assigned
```

The tick does this for you (on both `start` and `resume`), and hands the story to
the launcher so the iteration runs the assigned model:

```sh
printf '%s' "$prompt" | ralph --launch-agent implementation --story story.json
```

### The pair alternates across newly started stories

The two profiles are a **pair**, and Ralph swaps which one implements and which
one reviews so authorship and review influence stay balanced over the backlog.
The resolved role order — the committed defaults, or the operator's
`--implementation`/`--review` order — is the first newly assigned story's order;
the next newly assigned story runs the same pair the other way round:

```
#61  impl claude-opus-5   review gpt-5-codex
#62  impl gpt-5-codex     review claude-opus-5
#63  impl claude-opus-5   review gpt-5-codex
```

Alternation advances **only when a story that carries no assignment starts**. A
resumed checkpointed story, a retried failed Attempt, and a further review round
all read their roles off the story's own labels, so no story ever swaps models
midway — and a resume never consumes the swap the next new story is owed. The
phase advances only once the assignment has actually been recorded, so a `gh`
outage cannot silently burn a swap.

The phase is loop-local durable state under the target repository's git dir, next
to the tick lock: it survives across ticks, never enters the working tree or the
backlog, and a missing or damaged file simply starts the alternation over.

To keep the roles fixed, commit `models.alternate: false`, or pass
`--fixed-roles` for one invocation:

```sh
ralph --assign-models story.json --fixed-roles   # this story keeps the resolved order
```

## Authoring the backlog

Stories are GitHub issues in a **canonical shape** so the selection engine can
read them. The bundled **`ralph-story` skill** (`skills/ralph-story/`) specializes
the `to-issues` workflow to emit exactly this shape — use it when planning work.

Every story needs:

- exactly one `state:` and one `type:` label (except Blockers), and at most one (optional) `prio:` label,
- a `## Acceptance Criteria` heading with at least one `- [ ]` checklist item,
- a `Depends on:` line (`None`, or `#`-prefixed issue numbers),
- for HIL stories, an additional `## Bench Test Procedure` section.

Issue body template:

```markdown
## What to build

A concise description of this vertical slice — the end-to-end behavior, not a
layer-by-layer implementation.

## Acceptance Criteria

- [ ] Criterion 1 (observable, CI-checkable for AFK)
- [ ] Tests pass

## Bench Test Procedure   ← HIL stories only

1. Numbered steps a human runs on the bench to verify hardware-coupled behavior.

Depends on: #12, #34      ← or `Depends on: None`
```

Well-formed examples ship under `skills/ralph-story/examples/` (an AFK story, a
HIL story, and a Blocker). Always lint before publishing:

```sh
ralph --lint-story path/to/story.json
# or: gh issue view N --json number,title,labels,body | ralph --lint-story -
```

## The `ralph` CLI

`bin/ralph` is the entrypoint. Each subcommand does one thing; the loop
orchestrator (`bin/ralph.sh`) and the agent stitch them together. Run
`ralph --help` for the full usage text.

| Command | What it does |
| --- | --- |
| `ralph --init [CONFIG]` | Bootstrap the superproject: idempotently create the canonical labels and, if missing, the base branch (off the default branch). Run once per repo before authoring stories. |
| `ralph --check-config [PATH]` | Validate `.ralph.yml` (default `./.ralph.yml`) against the schema. |
| `ralph --lint-story PATH` | Validate a story issue (gh JSON shape; `-` for stdin) against the canonical format. |
| `ralph --dry-run [PATH]` | Scan the backlog and print the next action (`resume #N` / `start #N` / `no-work` / `halt`), changing nothing. Reads a JSON backlog from `PATH`, or scans live via `gh`. |
| `ralph --branch-name STORY [CONFIG] [PRD] [--base]` | Print the story's working branch from `branch_pattern` — every story has one, Orphan or Feature. With `--base`, print what that branch is cut from and what its pull request targets instead: the base branch for an Orphan Story, the Feature integration branch (`feature_pattern`, from the PRD issue) for a Feature story. |
| `ralph --run-gating [CONFIG]` | Run the configured gating steps locally, in order, fail-fast. |
| `ralph --resolve-models [CONFIG] [--implementation KEY] [--review KEY] [--allow-same-model]` | Resolve the implementation/review roles to exact model identities from the committed catalog; an override by profile key wins over the default. Refuses a same-model pair without the acknowledgement. |
| `ralph --assign-models STORY [CONFIG] [--implementation KEY] [--review KEY] [--allow-same-model] [--fixed-roles]` | Record the story's implementation/review model identities as durable labels (`model:impl:<id>` / `model:review:<id>`, created on demand). Idempotent; an already-assigned story is never rewritten. An unassigned story takes its turn in the role alternation unless `--fixed-roles` (or `models.alternate: false`) holds the order. |
| `ralph --launch-agent ROLE [CONFIG] [--story PATH] [--implementation KEY] [--review KEY] [--allow-same-model]` | Launch one role's agent in a fresh process through its provider adapter. With `--story`, the story's recorded assignment picks the model. Prompt on stdin, output on stdout; exit code is the outcome (0 normal, 10 session exhaustion, 12 infrastructure failure). |
| `ralph --reassign-model STORY ROLE PROFILE [CONFIG] --reason TEXT [--allow-same-model]` | Replace one role's recorded model assignment. **Human-only** — nothing in the tick calls it: a provider outage is retried and resumed with the negotiation round intact, and substituting a model is a person's decision. Moves the assignment label to the new identity and records an audit comment carrying the previous identity, the new one and the reason. Refuses a role with no assignment to replace, a profile outside the allowlist, a no-op replacement, a missing reason, and a swap that would collapse both roles onto one identity. |
| `ralph --normalize-usage PROVIDER [PATH]` | Print one provider's reported token usage (JSON on stdin, or at `PATH`) in the neutral categories — input, cached input, reasoning, output, total — each marked `reported` or `unavailable`. A category the provider does not expose is never estimated or zero-filled. |
| `ralph --implementation-green STORY [CONFIG] [PRD]` | Push locally green AFK or HIL work to the Story's own branch, create or update the Story's own marked pull request against its base — the base branch for an Orphan Story, the Feature integration branch (created off the base branch on first use) for a Feature Story — and move the Story to `state:in-review`. Refuses `main` and unmarked existing PRs; never merges, closes, or enters awaiting-bench. |
| `ralph --review-context STORY PR ROUND [ROOT]` | Print the diff-first evidence bundle for one fresh Review Agent round, bound to the pull request's exact head: base/head diff, acceptance criteria, scoped `AGENTS.md`, `CONTEXT.md`/ADRs, CI status, durable PR discussion — no implementation session or Handoff. |
| `ralph --validate-review PAYLOAD [DIFF]` | Validate a Review Agent's structured result (`-` for stdin) against the versioned contract in [`docs/review-contract.md`](docs/review-contract.md) before it is rendered as a GitHub review. Names the offending field paths on rejection; with the reviewed `DIFF`, also rejects a source location the diff never touched. |
| `ralph --render-review REVIEW PR [DIFF]` | Render a validated review result onto its pull request: inline threads for located findings, review body for cross-cutting ones, and the one stable required check `ralph/model-review` carrying the verdict. Re-validates first; a result for a stale head posts nothing. |
| `ralph --review-round STORY [CONFIG] [ROOT] [--pr PATH]` | Run one Negotiation Round for a Story In Review: find its marked pull request, skip a head that already carries its review, then launch the Story's assigned Review Agent once — fresh, read-only, holding no GitHub credential — validate what comes back, and publish it. Runs concurrently with CI and never waits for a check. |
| `ralph --await-review STORY [CONFIG] [ROOT]` | Wait out one bounded review window inside the tick: poll durable GitHub state with backing-off intervals (`review.wait_minutes`, default 60), run whichever round the state calls for — a review of an unreviewed head, or an answer to one that requested changes — and otherwise just wait, with no context and no invocation. Exits 14 when the window closes, which is the tick's cue to write a Handoff. |
| `ralph --complete-story STORY [CONFIG] [ROOT] [--pr PATH] [--prd PATH]` | Complete a Story whose current head passed **both** halves of the gate: CI green, and the one `ralph/model-review` context satisfied by an approving model review or by a human's Approve. An AFK Story merges its own pull request into its own base per `branching.afk_merge` (default squash — the base gets one clean commit, the pull request keeps the whole negotiation as its audit history) and closes as Passing: into the base branch for an Orphan Story, into the Feature integration branch for a Feature Story, whose code reaches the base branch when the Feature merges (ADR-0006). A HIL Feature Story merges the same way and then records a bench anchor at the exact reviewed head and moves to `state:awaiting-bench`, still open, so the Stories after it can build on it; a HIL Orphan Story is not merged at all before verification. Never targets `main`. |
| `ralph --arbitrate-review STORY [CONFIG] [ROOT] [--pr PATH]` | Act on the human's native GitHub review. **Approve** is authoritative: it releases the `ralph/model-review` check on the reviewed head even over unresolved model findings, clears any escalation (`state:blocked` → `state:in-review`), and records the override on the Story naming the reviewer and what it overrode. **Request changes** is authoritative feedback: the Story returns to `state:in-review` and the assigned implementation model is launched with the human's own words, append-only like any fix round. An ordinary comment changes no label, check, or state. Each native review is acted on exactly once. |
| `ralph --blocked-stories [BACKLOG]` | Print the open `state:blocked` story numbers — the Stories a human may have arbitrated since the last tick. |
| `ralph --escalate-review STORY [CONFIG] [ROOT] [--pr PATH]` | End automated negotiation for one deadlocked Story: summarize every unsettled blocking finding on the pull request with both sides' arguments, request a native GitHub review from `notify.github`, and move the Story from `state:in-review` to `state:blocked`. One Story only — unrelated Stories stay selectable, and whether the loop halts remains `limits.circuit_breaker`'s decision, which this newly blocked Story now counts toward. |
| `ralph --respond-review STORY [CONFIG] [ROOT] [--pr PATH]` | Answer a round that requested changes: launch the assigned implementation model with the open findings, verify the answer covers every one of them and that the new head is a *descendant* of the reviewed commit (an amend or force-push is refused, nothing posted), then push the appended fixes, reply in each finding's thread, and record one consolidated machine-readable response. A finding may be **disputed** with evidence instead of obeyed; a dispute changes no code, and the same commit goes back to a fresh reviewer to withdraw or uphold the finding. |
| `ralph --complete-afk STORY [CONFIG]` | Auto-merge a green AFK story into base (per `afk_merge`) and close its issue. Never touches `main`. |
| `ralph --complete-hil STORY [CONFIG]` | Open a PR to base for a green HIL story and move it to `state:awaiting-bench`. Never merges or closes. |
| `ralph --checkpoint STORY SUMMARY [CONFIG]` | Write a Handoff: commit + push WIP to the story branch, post a summary comment, stop. |
| `ralph --resume STORY [CONFIG]` | Resume a checkpointed in-progress story (check out its branch, surface the latest Handoff). |
| `ralph --record-attempt STORY REASON [CONFIG]` | Record a failed Attempt; block the story at `max_attempts`. |
| `ralph --reset-on-block STORY REASON [CONFIG] [PRD]` | Demote a blocked Feature Story and say where its work is. Every story works on its own branch, so the work is already quarantined from its siblings: this pushes that branch, comments the reason and the branch name, and moves the story to `state:blocked`. The Feature branch is never rewound. An Orphan Story is unchanged (no commands). |
| `ralph --check-breaker [BACKLOG] [CONFIG]` | Trip the circuit breaker (apply `needs-human`, tag the handle) if enough stories are blocked. |
| `ralph --read-learnings DIR [ROOT]` | Print the nested `AGENTS.md` learnings to read at story start (nearest-first). |
| `ralph --learn-target PATH [ROOT]` | Print the nearest `AGENTS.md` to promote a learning to. |

**Implementation-green is symmetric; final completion is asymmetric:**

- Both types first follow `push → marked PR create/update → state:in-review`,
  on the Story's own branch and its own pull request. An unmarked human pull
  request is outside the automated-review boundary.
- After review, both types merge into the Story's own base — the base branch for
  an Orphan Story, the Feature integration branch for a Feature Story. AFK then
  closes as Passing; HIL moves to Awaiting Bench Verification without closing.
  The open HIL issue keeps dependents ineligible until a human closes it after
  bench verification, and a Feature is refused integration into the base branch
  while any of its HIL Stories is still open (ADR-0006).
- A HIL **Orphan** Story is the one exception that is not merged: nothing stands
  between it and the base branch, so it waits for the bench first.

The Python logic in `lib/` is pure (returns result objects; no I/O side effects),
and side-effecting commands use a **plan → run** split: a pure planner emits the
git/gh commands as argv lists (unit-tested), and a thin runner executes them
fail-fast. Every plan **refuses to operate on `main`**.

## Failure handling

- An **Attempt** is an iteration that ends *without the story reaching green*. A
  context-full checkpoint (Handoff) is **not** an Attempt.
- After `limits.max_attempts` failed Attempts, the story moves to
  `state:blocked`.
- When `limits.circuit_breaker` stories are blocked, the **circuit breaker**
  trips: Ralph applies the `needs-human` label, tags the configured GitHub
  handle, and the loop **halts** (the selector returns `halt`) until a human
  intervenes and resets.

## Memory & learnings

Ralph keeps durable knowledge in **nested `AGENTS.md` files** in the superproject
— conventions, gotchas, HAL patterns. It reads them nearest-first at story start
and promotes genuinely reusable discoveries at completion (story-specific notes go
on the issue instead). There is deliberately **no `progress.txt`**.

> Note: the `ralph/` directory in *this* repo is the snarktank-style build
> harness used to *construct* ai-utils itself — it does use a `progress.txt`.
> Don't confuse it with the tool being built, which ships none.

## Self-hosting: ai-utils' own deployment

When ai-utils is the checkout root it is its own target repository (ADR-0001
amendment), so it commits the same things any other target repository does. What
follows is **an instance of the public contracts, not a default** — a repository
that mounts ai-utils as a submodule inherits none of it and supplies its own.

| What | Where | ai-utils' choice |
| --- | --- | --- |
| Model profiles + role defaults | `.ralph.yml` | `claude-opus` (`claude-opus-5`) implements, `codex-high` (`gpt-5-codex`) reviews, alternating |
| Quality gate | `.ralph.yml` `gating` | one step, `test`, running `test/run.sh` |
| CI | `.github/workflows/test.yml` | the same `test/run.sh`, on pull requests to `develop`; job named `test` so its check matches the gating step |
| Protected control plane | `.ralph.yml` `control_plane` | the review machinery itself — prompts, schemas, `lib/ralph_review*.py`, the review contract, both entrypoints, `.ralph.yml`, and `.github/workflows/**` |
| Base branch | `.ralph.yml` `branching.base` | `develop`; promoting `develop` → `master` is human-owned |

**Branch protection on `develop`.** Two checks are required — `test` (CI) and
`ralph/model-review` (the verdict Ralph publishes) — and **no** approving reviews
are. Ralph opens its pull requests with the operator's own credential and cannot
approve them, so requiring an approval would turn every AFK Story into a
human-gated one; human approval where it genuinely matters (a protected-path
change, a deadlock, an override) is enforced by Ralph itself, not by GitHub.
Administrators stay outside the restriction, so a human can still merge a
feature-integration pull request (ADR-0006), which carries no model review of its
own. A **feature branch needs no protection of its own** — every Story that
merges into one was reviewed and CI-checked on its own pull request first, and
the base branch is where the gate has to hold. To reproduce the setting:

```sh
gh api -X PUT repos/OWNER/REPO/branches/develop/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": false,
    "contexts": ["test", "ralph/model-review"]
  },
  "required_pull_request_reviews": null,
  "enforce_admins": false,
  "restrictions": null
}
JSON
```

**Upgrading a repository that ran the shared-pull-request topology.** Drop
`branching.rescue_pattern` from `.ralph.yml` — it is no longer a schema key, and
`ralph --check-config` names it. Then close or retarget by hand any still-open
shared Feature pull request; its Feature's remaining Stories pick up the new
topology at their next start, and nothing is re-implemented. The full migration
is written up in
[ADR-0006](docs/adr/0006-per-feature-integration-branches.md#migrating-a-feature-already-in-flight).

**The pushing credential needs `workflow` scope.** GitHub refuses a push that
creates or updates anything under `.github/workflows/` from a token without it,
so once a target repository carries CI, the credential Ralph pushes with must
have it or a Story touching CI cannot land its own branch:

```sh
gh auth refresh -h github.com -s workflow
```

**The reviewer's credential boundary.** The Review Agent is launched read-only
and holds no GitHub identity: the adapter strips `GH_TOKEN`, `GITHUB_TOKEN` and
`GH_ENTERPRISE_TOKEN` from its environment *and* points `GH_CONFIG_DIR` at an
empty directory, so `gh`'s file-based credential (`~/.config/gh/hosts.yml`) is
out of reach too. Everything the reviewer says reaches GitHub through the trusted
wrapper, which validates it first. This part is shipped behaviour, not an
ai-utils setting — every target repository gets it.

## Project layout

```
bin/
  ralph          Bash CLI entrypoint — dispatches subcommands to lib/.
  ralph.sh       The unattended tick: flock → validate → select → iterate → complete.
lib/*.py         Pure logic (Python 3, stdlib + jsonschema + PyYAML). No network, no side effects.
  ralph_config.py   .ralph.yml validation + default application.
  ralph_story.py    Canonical story-format checker + label normalization.
  ralph_select.py   Selection engine: normalize → select_next → Action.
  ralph_iterate.py  Branch naming + local gating runner.
  ralph_review.py   Durable marked-PR identity for automated review opt-in.
  ralph_implementation.py  Green implementation → marked PR + In Review.
  ralph_afk.py      AFK completion (auto-merge + close).
  ralph_hil.py      HIL completion (PR + awaiting-bench).
  ralph_handoff.py  Checkpoint/resume (never compacts).
  ralph_failure.py  Attempt counting + circuit breaker.
  ralph_memory.py   Nested AGENTS.md read/promotion.
schema/          Shipped JSON-schemas (ralph.schema.json for .ralph.yml).
prompts/         Checked-in agent prompts (iterate/handoff/failure/memory), drift-guarded by tests.
skills/          Authoring skills shipped with the tool (ralph-story + examples).
scheduler/       Sample scheduler units (systemd ralph.service + ralph.timer, ralph.cron) — a tick every 5h.
docs/adr/        Architecture Decision Records (0001–0005).
test/            Green gate: test/run.sh, unit tests, fixtures, optional bats.
.github/workflows/  ai-utils' own CI (self-hosting only — not inherited by a submodule mount).
.ralph.yml.sample  Documented sample config (a test asserts it validates).
```

## Running the tests

```sh
test/run.sh
```

`test/run.sh` is the green gate. `test/unit/` uses Python's stdlib `unittest`
(no `pytest` needed); `test/bats/` holds bats orchestration tests that are
auto-skipped if bats isn't installed. Fixtures live under `test/fixtures/`.

## Design decisions

The rationale behind the architecture is recorded as ADRs in `docs/adr/`:

- **0001** — ai-utils as config-driven tooling submodule (fail-loud config).
- **0002** — Labels as the backlog source of truth (not a Projects board).
- **0003** — TDD off-target against a fakeable HAL.
- **0004** — No compaction: terminate-and-resume loop.
- **0005** — Two-tier memory, no `progress.txt`.

See `CONTEXT.md` for the full glossary (and note: it's **HIL**, never *HITL*).
