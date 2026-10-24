# The tick and operations — module notes

Notes for `bin/ralph.sh`, `lib/ralph_tooling.py`, `lib/ralph_pointer.py`, `lib/ralph_init.py`, `scheduler/`, and `ai-utils`' own deployment. Read this before changing those modules; it is part of
`AGENTS.md` (the root file is the index), split out so an agent loads only the notes
for the code it touches. Add a new learning here, in the file that owns it.

- `bin/ralph.sh` is the unattended **tick** (US-011, ADR-0002/0004/Tick): thin orchestration —
  the TDD/gating happen inside the `claude` iteration it launches (driven by
  `prompts/iterate.v1.md`), but the tick owns the state transitions around it. Order: (1)
  resolve, provision and enter its **Slot**, then `flock -n` that Slot's lockfile in the git
  common directory so only one tick per *Slot* runs — an overlapping tick logs "already
  running in slot N" and exits 0 (the Slot, below); (2)
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
- **The Slot a tick runs in** (#134, ADR-0008, PRD #133). `lib/ralph_slot.py` decides it;
  `bin/ralph.sh` (`resolve_slot`, `enter_slot`, `sync_slot`) does it. A **Slot** is a git
  worktree dedicated to one Loop at a sibling path of the repository,
  `<repo>.ralph-slot-<N>` — `N` from `RALPH_SLOT` (default 1, the scheduler's template
  instance; exported, so the agents a tick launches run under the same Slot), the path
  overridable outright by `RALPH_SLOT_DIR`. A tick is still launched from the target
  repository: it resolves its Slot (`ralph --slot` prints `NUMBER LOCK PATH`), takes the
  lock, provisions or repairs the worktree (`ralph --slot-ensure PATH [COMMITISH]` prints
  `created|repaired|reused`), `cd`s into it, and does **all** of its git work there, so the
  operator keeps their own checkout, branch and working tree through a tick. `slot_number`,
  `slot_dir`, `lock_file` and `action` are pure; only `resolve` and `ensure` run git. The
  detach and the reset are the tick's own `git`, in the Slot it has already entered.
  Order inside the tick: resolve → lock → provision+enter → mid-operation refusal →
  detach+sync. Covered by `test/unit/test_slot.py` (pure decisions, plus the real
  `bin/ralph.sh` on real git in the self-hosted world, which is the only thing that can show
  a worktree, an untouched operator checkout, and a mid-merge Slot told apart from a
  mid-merge checkout), the lock and mid-merge cases in `test/bats/orchestration.bats` /
  `test/bats/story_branch.bats`, and the Slot-shaped mock `git` in both tick harnesses.
  GOTCHAS: (1) the lock is `<git-common-dir>/ralph-tick-slot-<N>.lock`, in the **common**
  directory, because every worktree of one clone must agree on it; two ticks in the same
  Slot still serialize, and Slot 2 is not blocked by Slot 1. `RALPH_LOCK_DIR` still
  overrides the directory (there is no `.git` default any more: inside a linked worktree
  `.git` is a *file*). The Alternation phase moved to the same directory for the same
  reason (`docs/agents/agents.md`). (2) `sync_slot` detaches HEAD at the start of every
  tick — a crashed earlier tick cannot keep holding a Story's branch, so any Slot can check
  it out — and then hard-resets to `origin/<base>`. The reset is what keeps the `.ralph.yml`,
  schema and gating a tick validates the **base's**: a Slot created once and only reused
  would otherwise read the config of whenever it was created for ever, while frozen tooling
  ran `origin/<base>`'s code against it. Nothing is lost — a Story's work lives on its own
  pushed branch. The base comes from `SLOT_BASE`, read from the operator's config *before*
  the `cd` (the Slot's copy is the thing being replaced; a config too broken to read falls
  back to `main`), and the `fetch` before the reset is best-effort. (3) The Slot is prepared
  by the **pre-freeze** tick only: the frozen copy inherits the held lock on fd 9 *and* a
  Slot already checked, detached and reset, and preparing it again would be a second tick's
  worth of git. (4) A Slot is never the operator's checkout — `ensure` refuses the clone's
  main worktree and the repository it is called from, whatever `RALPH_SLOT_DIR` says — and a
  Slot number that could climb out of its path is refused by `slot_number`. (5) A deleted
  Slot directory is still *registered*, so `ensure` runs `git worktree prune` before
  `worktree add`; a directory that is no longer a worktree of this clone is removed and
  remade (`repaired`). (6) Which ai-utils runs is ADR-0009's decision, not the Slot's:
  `ralph_tooling` and `ralph_pointer` resolve against `ralph_slot.main_worktree`, the clone's
  main worktree, so they answer the same from the operator's checkout, from a Slot and from a
  frozen copy. (7) A **pre-#134** Tooling Commit cannot run in a Slot: its lock path is
  `.git/ralph-tick.lock`, and `.git` is a file there, so its preflight fails and the pin is
  refused, naming the step. That is deliberate — exporting `RALPH_LOCK_DIR` to the common
  dir instead would let such a copy take a *different* lock and run a whole tick as its
  "preflight".
- **Frozen tooling** (#146, ADR-0009, PRD #145). `lib/ralph_tooling.py` resolves the
  **Tooling Commit** (`ralph --tooling-commit`): self-hosted (ai-utils is the checkout
  root) is `origin/<base>` after a fetch; a Superproject is the gitlink at the mount in
  `origin/<base>`'s tree, never the index or the submodule checkout; ai-utils installed
  outside the checkout, or `tooling.allow_pointer_drift`, prints `unfrozen` and the tick
  runs as before (that is the opt-out: the mounted checkout runs, the pointer check warns).
  `ralph --tooling-copy` makes the copy once -- a detached worktree of the repository that
  owns ai-utils (the checkout itself, or the submodule's repository) at
  `<git-common-dir>/ralph-tooling/<commit>` -- and a half-made one is replaced, never
  reused. The tick (right after the lock and the mid-operation check) preflights the copy
  with `<copy>/bin/ralph.sh --preflight` (config check, subcommand preflight, pointer
  check, dry run; the failed step is the `RALPH-PREFLIGHT-FAILED:` line, and a copy that
  crashes before naming one is "the tick script") and then `exec`s the copy's `ralph.sh`.
  A failing preflight refuses (exit 2) naming the commit and step, before anything is
  labelled. GOTCHAS: (1) the exec marker `RALPH_TOOLING_COMMIT` (with `_KIND`, `_MOUNT`)
  is read into shell variables and **unset** at the top of the script, so no agent or test
  suite inherits it; the tick harnesses strip it too. (2) The frozen tick does not reopen
  its lock: fd 9 survives the exec and is still the held lock, and re-opening it would
  release it for a moment. (3) The copy's path sits in a git directory, not at the mount,
  so the frozen tick hands `--check-pointer` the commit and mount through those variables;
  it then compares with `origin/<base>`'s gitlink (falling back to `HEAD`'s). (4) The
  bootstrap up to the exec is the *checkout's* code, the one part frozen tooling cannot
  cover; keep it small. (5) A Tooling Commit older than #146 has no `--preflight` mode: its
  "preflight" is a whole tick, which exits 0 at once on the held lock, so it passes and the
  old copy then runs as it always did (a Superproject pinning a pre-freeze ai-utils). (6)
  `ralph_agent.TOOLING_BIN` (the launcher's own `bin/`) is put
  first on every agent's `PATH`, for every adapter and role. Covered by
  `test/unit/test_frozen_tooling.py` and `test/bats/frozen_tooling.bats` (self-hosted, real
  git), and the scenario worlds `pointer_drift` (`mount_ai_utils: moved`: the checkout
  under the pin cannot select work, and the pin still runs) and
  `pointer_pin_fails_preflight` (`broken-pin`).
- **Pruning frozen copies** (#147). The pre-freeze tick takes `<copies>/<commit>.lock`
  **shared** on fd 8 (`hold_copy`) *before* it makes the copy, and the exec carries it,
  so a tick holds its copy for as long as it runs. After its preflight the frozen tick
  runs `ralph --prune-tooling <copies> <commit>`: `ralph_tooling.prune` takes every
  candidate's lock **exclusively** (non-blocking; a lock it cannot get is a copy in use),
  asks the pure `prune_plan(copies, keep, last_known_good, in_use)`, and removes what it
  names while still holding those locks, so a tick that wants a copy back waits and then
  recreates it. Best-effort: a copy it cannot remove is logged and retried by a later
  tick. GOTCHAS: (1) the lock files are never deleted: unlinking one would let a later
  tick lock a fresh file while an earlier one still holds the old inode. (2) `git
  worktree remove` runs from a copy that stays (a worktree of the same repository),
  never from inside the git directory.
- **Last-Known-Good** (#148). Self-hosted only: a Tooling Commit whose preflight passes is
  written to `<copies>/last-known-good` (tmp + `mv`) before the exec. One that fails makes
  the pre-freeze tick lock, make and preflight the Last-Known-Good copy and exec *that*,
  exporting `RALPH_TOOLING_FAILED`/`_FAILED_STEP` (unset at the top of the script, like the
  other markers), so the frozen tick logs both commits and the step, and keeps the failing
  copy when it prunes, because it is still the current Tooling Commit and the next tick
  retries it. No Last-Known-Good (or it is the failing commit itself), or a Last-Known-Good
  that fails preflight too (the repository's own config broke, say), exits 2 naming the
  commits and steps. A Superproject never reads the file: a failing pin is refused.
  Covered by `SelfHostedTicksFallBackToLastKnownGood` and the scenario test that plants a
  `last-known-good` under a failing pin.
- **The fallback issue** (#149) is `lib/ralph_fallback.py` (`ralph --fallback-issue`):
  pure `plan(event, issue, ...)` -> gh argv lists, found again by the hidden
  `<!-- ralph-tooling-fallback failed=<sha> -->` marker (never the title). A fallback tick
  opens it (no labels at all, so selection never takes it), or edits the body of the open
  one, adding a comment only when a *different* commit fails; the first passing tick after
  that comments the commit and closes it. The frozen tick runs it from `report_fallback`,
  next to pruning, with the copy's own `ralph`. GOTCHAS: (1) `<copies>/fallback-open` is
  set by every fallback tick and removed only once the close succeeded, so a healthy tick
  makes no GitHub call for it and a failed close is retried. (2) Best-effort: a gh error is
  logged ("could not report the fallback on its issue") and the tick goes on, because it is
  the tick keeping the Loop alive. The full cycle runs against the scenario tier's stateful
  fake gh (`SelfHostedWorld.use_fake_gh`), which emulates `issue create` and
  `issue edit --body` for it.
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
  agent is not launched. (2) The tick refuses (exit 2) a **Slot** that is mid-merge/rebase/
  cherry-pick or has unmerged paths, *before* `--check-config`, so the cause is named. Since
  #134 it reads the Slot's per-worktree git dir only: the operator's own half-finished merge
  is none of its business and no longer stops a tick.
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
  in the Superproject's **committed tree** (`git ls-tree HEAD -- <path>`, never the index; under
  frozen tooling the frozen commit with `origin/<base>`'s gitlink, see above) and
  exits 2 on a mismatch naming both commits, unless `tooling.allow_pointer_drift` (schema
  default false) makes it a logged warning. `locate` returns None when ai-utils is the checkout
  root (self-hosting) or not inside it, and the check passes. The tick runs it right after the
  subcommand preflight, before anything is labelled. Scenario worlds cover it with
  `mount_ai_utils: committed|drifted|moved|broken-pin` (a copy of the working tree as a nested
  repo recorded by a gitlink; `drifted` commits once more under it, `moved` breaks the checkout
  under the pin, `broken-pin` pins a commit no config passes) and `tick_exit`, the exit a
  refusing Tick owes. Since #146 a drifted checkout no longer refuses by default: the frozen
  pin runs, and only `allow_pointer_drift` runs (and warns about) the checkout itself.
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
  a permissions problem; check the scope before chasing the diff. (5) A pull request Ralph does not
  own, i.e. one built by hand, gets `ralph/model-review` = success from
  `.github/workflows/model-review-waiver.yml`, which asks `ralph_review.owned_by_ralph(head,
  body, branching)` (marker, or a head on the Story/Feature branch patterns, so editing the
  marker out of a Ralph PR's body does not waive it). It runs on every pull request, not
  just those into `main`: a GitHub stack holds each of its pull requests to `main`'s rules,
  and hand-built stacks could otherwise only merge by admin bypass.
