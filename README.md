# ai-utils — the Ralph Loop

Reusable, project-agnostic tooling shared across embedded projects as a **git
submodule**. Its centerpiece is the **Ralph Loop**: an autonomous coding-agent
loop that works through a GitHub-issue backlog, implementing stories test-first
until the local quality gate is green.

ai-utils contains no project-specific source, issues, or CI. You mount it inside
a host project (the *superproject*) and drive it from there. Ralph only ever
modifies the superproject — never ai-utils itself, and **never `main`**.

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
4. When green, either **auto-merge** it (software-only stories) or **open a PR**
   and hand it to you for **bench verification** (hardware-coupled stories).
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
| **AFK story** (`type:afk`) | *Away-From-Keyboard.* Fully verifiable by CI alone (logic, parsing, refactors, build config). Done as soon as the gate is green — Ralph auto-merges it. |
| **HIL story** (`type:hil`) | *Human-In-the-Loop.* Needs a human to confirm real behavior on the physical bench (GPIO, timing, sensors). Green CI is necessary but **not** sufficient; Ralph opens a PR and waits for bench verification. |
| **Blocker** | A story needing a human *design decision before coding.* Kept out of `state:ready` (labelled `ready-for-human`) so Ralph never picks it up. |
| **Tick** | One scheduled run of the loop (every ~5 hours). Resumes any in-progress story first, then works as many ready stories as the session budget allows. Only one tick per superproject runs at a time (guarded by a `flock`). |
| **Iteration** | A single fresh-context agent process inside a tick. Ralph **never compacts context** — when it fills, the iteration writes a *Handoff* and the next iteration resumes with clean context. |
| **Handoff** | The checkpoint an iteration leaves so a story can resume: a summary as an issue comment + WIP commits on the story branch. |
| **Gating steps** | The build/test/lint checks Ralph must pass before a story counts. You declare them in `.ralph.yml`. |
| **Learnings** | Durable, reusable knowledge Ralph records in nested `AGENTS.md` files in the superproject (there is no `progress.txt`). |

### The label scheme (source of truth)

The backlog lives entirely in GitHub Issue **labels + body conventions** — this
is what Ralph reads, not a Projects board. The scheme is **mandated and not
configurable**:

- **State** (exactly one): `state:ready` → `state:in-progress` → `state:awaiting-bench` → *closed* (= Done).
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
   fresh-context `claude` iteration  (prompts/iterate.v1.md)
        │   implements the story test-first, runs gating
        ├── green + AFK  ──► ralph --complete-afk   (auto-merge → base, close issue)
        ├── green + HIL  ──► ralph --complete-hil   (open PR, → state:awaiting-bench)
        ├── context full ──► ralph --checkpoint     (Handoff, resume next iteration)
        └── failed       ──► ralph --record-attempt (block after max_attempts)
        │
        ▼
   loop to next eligible story until no-work / halt / session budget spent
```

## Requirements

- **Bash** and **Python 3** (stdlib + [`PyYAML`](https://pypi.org/project/PyYAML/) + [`jsonschema`](https://pypi.org/project/jsonschema/)).
- The **[`gh`](https://cli.github.com/) GitHub CLI**, authenticated for the superproject (Ralph reads the backlog and opens PRs through it).
- **[Claude Code](https://claude.com/claude-code)** (`claude` on `PATH`) — Ralph drives it to do the actual implementation.
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

Once installed, Ralph wakes every 5 hours, resumes any in-progress story, works
as many ready stories as the session budget allows, then sleeps until the next
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
  branch_pattern: "ralph/{issue}-{slug}"   # {issue}/{slug} substituted
  afk_merge: squash                        # merge | squash | rebase (default: squash)

# Failure-handling limits.
limits:
  max_attempts: 3      # failed Attempts before a story → state:blocked (default: 3)
  circuit_breaker: 2   # blocked stories that halt the loop + tag a human (default: 2)

# Who gets tagged when the circuit breaker trips (needs-human).
notify:
  github: your-github-handle   # no leading @
```

`version`, `gating`, and `notify` are required; everything else has sensible
defaults. See `.ralph.yml.sample` for the annotated original.

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
| `ralph --branch-name STORY [CONFIG]` | Print the story branch name from `branch_pattern`. |
| `ralph --run-gating [CONFIG]` | Run the configured gating steps locally, in order, fail-fast. |
| `ralph --complete-afk STORY [CONFIG]` | Auto-merge a green AFK story into base (per `afk_merge`) and close its issue. Never touches `main`. |
| `ralph --complete-hil STORY [CONFIG]` | Open a PR to base for a green HIL story and move it to `state:awaiting-bench`. Never merges or closes. |
| `ralph --checkpoint STORY SUMMARY [CONFIG]` | Write a Handoff: commit + push WIP to the story branch, post a summary comment, stop. |
| `ralph --resume STORY [CONFIG]` | Resume a checkpointed in-progress story (check out its branch, surface the latest Handoff). |
| `ralph --record-attempt STORY REASON [CONFIG]` | Record a failed Attempt; block the story at `max_attempts`. |
| `ralph --check-breaker [BACKLOG] [CONFIG]` | Trip the circuit breaker (apply `needs-human`, tag the handle) if enough stories are blocked. |
| `ralph --read-learnings DIR [ROOT]` | Print the nested `AGENTS.md` learnings to read at story start (nearest-first). |
| `ralph --learn-target PATH [ROOT]` | Print the nearest `AGENTS.md` to promote a learning to. |

**Completion is asymmetric by design:**

- **AFK** → `push → gh pr create (Closes #N) → gh pr merge → gh issue close`. The
  closed issue is what makes dependents eligible.
- **HIL** → `push → gh pr create (Refs #N) → label state:awaiting-bench`. Ralph
  never merges or closes; the human bench-verifies and merges the clean diff. The
  issue stays open, so dependents stay ineligible until the human closes it.

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
