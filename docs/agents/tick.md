# The tick and operations — module notes

Notes for `bin/ralph.sh`, `lib/ralph_pointer.py`, `lib/ralph_init.py`, `scheduler/`, and `ai-utils`' own deployment. Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

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
  `test/bats/orchestration.bats` (bats is required by `test/run.sh`) AND
  `test/unit/test_orchestrate.py` (driving the
  script against mock `claude`/`gh`/`git` on PATH via `$RALPH_LOG` + a stateful `gh issue list`
  queue that pops one backlog fixture per call to simulate stories completing).
- **The Story's branch before any git** (#121). A `start`/`resume` resolves the Story's branch
  and what it is cut from with `ralph --branch-name` (the iterate prompt's own CLI; a Feature
  Story's PRD is fetched for it), then `checkout_story_branch` puts the checkout there: hard-
  synced to `origin/<branch>` unless the local branch is origin plus unpushed commits (kept),
  else the local branch, else cut from `origin/<cut-from>` (`origin/<base>` while the Feature
  branch does not exist). Only then does `freshness_merge` run, merging **`origin/<base>`**
  into the Story branch, and only when that branch does not already contain it. GOTCHAS:
  (1) the old `sync_branch`/`freshness_merge` acted on whatever `HEAD` the previous iteration
  left -- another Feature's Story branch -- and a conflict there left markers in the tracked
  `.ralph.yml`, so every later tick died as INVALID CONFIG (autopilot_controller #183). A
  conflict is now `git merge --abort`ed and recorded as a failed Attempt naming the paths; the
  agent is not launched. (2) The tick refuses (exit 2) a checkout that is mid-merge/rebase/
  cherry-pick or has unmerged paths, *before* `--check-config`, so the cause is named.
  (3) `--needs-freshness` is asked before the branch is resolved, so the `gh issue list` reads a
  tick makes do not depend on whether resolution succeeds (the mocked queues count them). An
  unresolvable branch leaves git untouched and the iteration resolves it itself. (4) The
  freshness tick tests and `test/bats/story_branch.bats` run on **real git**: a logging mock
  answers "already an ancestor" to everything and cannot show which branch a merge hit.
- **Reconciliation runs however the story loop ends** (#120). `tick()` validates, then calls
  `work_backlog` (the story loop; every early end of the tick -- a Story left in review, a
  session limit, a failed promotion, a halt, a launcher refusal -- is a `return` from it), and
  *then* `completion_pass` and `arbitration_pass`, returning the loop's code. They used to sit
  after a clean `no-work` exit only, so with story work in the backlog a finished Feature never
  integrated. They still run *after* the loop, not before it: the queue-driven tick harnesses
  count `gh issue list` reads, and a pass at the start would shift every test's queue.
  `ralph_feature.feature_complete_plan` (same issue) is now sync → autosquash → rebase → gating
  → **push** → pr-create → prd-close → cleanup: it checks out `feature/...` hard-synced to
  origin whatever `HEAD` was, autosquashes and rebases onto **`origin/<base>`** (the local base
  is never fetched into), and pushes the gated branch `--force-with-lease` by full ref name
  before `gh pr create`, so what merges is what was gated. The blocker path pushes the same
  way (never `HEAD:`, never `-u`). `gh pr merge` no longer passes `--delete-branch` (the
  branch is checked out here); `cleanup` deletes the remote branch by name, best-effort.
- `lib/ralph_pointer.py` is the **Superproject pointer check** (#96): `ralph --check-pointer
  [CONFIG]` (dispatched with `$RALPH_HOME`) compares the running ai-utils `HEAD` with the gitlink
  in the Superproject's **committed tree** (`git ls-tree HEAD -- <path>`, never the index) and
  exits 2 on a mismatch naming both commits, unless `tooling.allow_pointer_drift` (schema
  default false) makes it a logged warning. `locate` returns None when ai-utils is the checkout
  root (self-hosting) or not inside it, and the check passes. The tick runs it right after the
  subcommand preflight, before anything is labelled. Scenario worlds cover it with
  `mount_ai_utils: committed|drifted` (a copy of the working tree as a nested repo recorded by a
  gitlink; `drifted` commits once more under it) and `tick_exit`, the exit a refusing Tick owes.
- `lib/ralph_init.py` is the one-shot bootstrap seam (`ralph --init`): same pure-`Plan`/
  `run_plan`/CLI shape as the completion stages. `init_plan(base, base_exists, default_branch,
  prio_max)` returns the ordered gh/git commands to `gh label create --force` the canonical
  vocabulary (`FIXED_LABELS` — the exact `state:`/`type:`/`needs-human`/`ready-for-human`
  labels `ralph_story`/`ralph_select` consume — plus a `prio:0..N` starter range) and, when
  `base` is missing, create it off the default branch (never fabricating `main`, which always exists).
  The CLI detects live state (`_remote_has_branch`, `_default_branch`) then runs the plan.
  Idempotent by construction (`--force`, skip-if-present). It also links every shipped
  skill (`skills/<name>/SKILL.md`) into the project's `.claude/skills/<name>` — that is the only
  place Claude Code discovers project skills (the `skills/` folder they ship in is not one of
  them), which is how a skill added after the hand-made links (`ralph-backlog-audit`) went
  unloaded. Links are
  relative (they survive a clone) and any name already present is skipped, so a project's
  own skill is never clobbered and a re-run links only new ones. Covered by `test/unit/test_init.py`.
- `scheduler/` ships the sample schedulers (US-012, ADR-0001): systemd `ralph.service`
  (`Type=oneshot`, `ExecStart=.../bin/ralph.sh`) + `ralph.timer` (`OnCalendar=*-*-* 00/5:00:00`,
  i.e. every 5h) and a `ralph.cron` one-liner (`0 */5 * * *`). Both just fire the flock-guarded
  tick. Green gate is a DRIFT-GUARD (`test/unit/test_scheduler.py`), same idea as the prompt
  tests: assert the files exist and still carry the 5-hour cadence + `ralph.sh` ExecStart, and
  that the README's install section covers submodule/config/schedule/`gh auth login`+`claude`
  auth. GOTCHA: do NOT `assertNotIn("HITL", README)` — the README intentionally says "never
  *HITL*", so that check false-positives (the substring rule bites again, cf. the prompt note).
- **`ai-utils`' own deployment** (#64, PRD #42) is an *instance* of the public contracts,
  never a default a submodule mount inherits: `.ralph.yml` (profiles, defaults, protected
  paths, `notify`), `.github/workflows/test.yml` (CI — the same `test/run.sh` the gating
  step runs, job named `test` so the check name matches the gating step name), and
  `main`'s branch protection. README's *Self-hosting* section carries the protection
  settings as a reproducible `gh api` call. GOTCHAS: (1) `main` requires the `test` and
  `ralph/model-review` contexts but **no approving reviews** — Ralph opens its pull requests
  with the operator's own credential and cannot approve them, so a required approval would
  human-gate every AFK Story; the approvals that matter (protected path, deadlock, override)
  are Ralph's own gate, not GitHub's. (2) `enforce_admins` stays **false** so a human can
  merge a feature-integration pull request (ADR-0006), which carries no `ralph/model-review`
  check of its own and would otherwise be unmergeable. (3) `.github/workflows/**` is itself
  protected: the CI the gate reads must not be weakenable by an unattended Story. (4) Once a
  target repository carries CI, the credential the Implementation Agent pushes with needs
  GitHub's **`workflow` scope** — a push that creates or updates anything under
  `.github/workflows/` is rejected without it (`gh auth refresh -h github.com -s workflow`).
  The push fails at the git layer, so it looks like an ordinary Attempt failure rather than
  a permissions problem; check the scope before chasing the diff.
