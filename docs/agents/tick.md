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
- **`ai-utils`' own deployment** (#64, PRD #42) is an *instance* of the public contracts,
  never a default a submodule mount inherits: `.ralph.yml` (profiles, defaults, protected
  paths, `notify`), `.github/workflows/test.yml` (CI — the same `test/run.sh` the gating
  step runs, job named `test` so the check name matches the gating step name), and
  `develop`'s branch protection. README's *Self-hosting* section carries the protection
  settings as a reproducible `gh api` call. GOTCHAS: (1) `develop` requires the `test` and
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
