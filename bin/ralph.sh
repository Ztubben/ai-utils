#!/usr/bin/env bash
# ralph.sh - the Ralph Loop unattended tick (US-011, ADR-0002/0004).
#
# One tick is a single scheduled run (the scheduler fires it every 5 hours,
# US-012). A tick:
#   1. resolves its Slot (ADR-0008) -- a worktree dedicated to one Loop at
#      <repo>.ralph-slot-<N> -- provisions or repairs it, and guards it with
#      flock so only one tick per *Slot* runs at a time; an overlapping tick in
#      the same Slot exits immediately (lockfile in the git common directory).
#      Everything after that runs in the Slot, never in the operator's checkout.
#   2. validates .ralph.yml at tick start (fails loud, ADR-0001).
#   3. drives the pure selection engine (`ralph --dry-run`) which is resume-first:
#      any active Story (state:in-progress or state:in-review) is resumed before
#      scanning for new state:ready
#      work.
#   4. launches a fresh-context Implementation Agent iteration per selected
#      story -- through the provider-neutral adapter interface, so which
#      provider runs is the target repository's model catalog's decision, not
#      this script's -- and works as many eligible stories in sequence as the
#      session budget allows, until no eligible work remains (no-work) or the
#      loop halts (needs-human).
#   5. when an iteration signals the story is green (the done-signal marker),
#      promotes it with `ralph --implementation-green`: both AFK and HIL open
#      or update a marked pull request and enter state:in-review, using the
#      canonical Story/Feature branch resolution from ADR-0006.
#      Without this promotion the still-in-progress story would be re-selected
#      forever (the engine is resume-first), so a green story must move off the
#      backlog before the loop advances.
#   6. when the agent signals session-limit exhaustion, checkpoints the current
#      story via a Handoff (`ralph --checkpoint`) and ends the tick cleanly.
#
# Before any of that, the tick freezes its tooling (ADR-0009): it resolves the
# Tooling Commit (origin/<base> when ai-utils is the checkout root, the pinned
# gitlink in origin/<base>'s tree in a Superproject), makes sure an immutable
# copy of that commit exists, preflights it (`ralph.sh --preflight`), and
# re-execs the copy's ralph.sh exactly once with the tick lock still held. From
# then on the tick, and every agent it launches, runs the frozen copy -- never
# the checkout the agent is editing, never a submodule checkout that moved.
#
# Ralph only ever modifies its target repository (the superproject when ai-utils is
# mounted as a submodule; ai-utils itself when it is the checkout root -- ADR-0001
# amendment) and never commits on main itself. The heavy
# lifting (TDD, gating) lives in the agent iteration driven by
# prompts/iterate.v1.md, which reports a green story via a done-signal marker;
# this script is only the orchestration shell -- selecting, promoting green
# stories, and checkpointing -- kept thin so it can be driven by tests against
# mocked provider CLIs/`gh`/`git` on PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_BIN="${RALPH_CLI:-$SCRIPT_DIR/ralph}"
ITERATE_PROMPT="$SCRIPT_DIR/../prompts/iterate.v1.md"

# Tunables (env-overridable so tests and superprojects can adjust them).
# The Slot this tick runs in (ADR-0008): the scheduler's template instance, or 1
# for the single-instance units and the cron line. Exported, so the agents this
# tick launches run under the same Slot as the tick that launched them.
: "${RALPH_SLOT:=1}"
export RALPH_SLOT
# RALPH_LOCK_DIR overrides where the per-Slot tick lock lives; unset, it is the
# clone's git common directory, which every worktree of the clone agrees on.
: "${RALPH_CONFIG:=.ralph.yml}"
: "${RALPH_MAX_ITERATIONS:=25}"             # safety bound on stories per tick
# Session-limit detection lives in lib/ralph_session.py (#65), so the tick and
# the provider adapters read one answer. The tick never classifies directly: the
# adapter behind `ralph --launch-agent` calls that module and reports the verdict
# as its exit code. The knobs (RALPH_SESSION_LIMIT_EXIT,
# RALPH_SESSION_LIMIT_MARKER) are read from the environment there; no defaults
# are pinned here.
: "${RALPH_STORY_COMPLETE_MARKER:=RALPH-STORY-COMPLETE}"  # iteration's green/done-signal (prompts/iterate.v1.md)
: "${RALPH_NEEDS_HUMAN_MARKER:=RALPH-NEEDS-HUMAN:}"       # iteration's "only a human can unblock this" signal (#114)
# The identity every token-usage event of this tick is stamped with (#62), so a
# later script can group one run's invocations across the Stories it worked.
: "${RALPH_RUN_ID:=tick-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
export RALPH_RUN_ID

log() { printf 'ralph: %s\n' "$*"; }

# The frozen tooling this process runs, when the pre-freeze tick exec'd it (or
# asked it for a preflight). Read once and un-exported, so the agents and test
# suites this tick launches never inherit the marker that stops the re-exec.
TOOLING_COMMIT="${RALPH_TOOLING_COMMIT:-}"
TOOLING_KIND="${RALPH_TOOLING_KIND:-}"
TOOLING_MOUNT="${RALPH_TOOLING_MOUNT:-}"
# Set when this is Last-Known-Good, run because the Tooling Commit failed (#148).
TOOLING_FAILED="${RALPH_TOOLING_FAILED:-}"
TOOLING_FAILED_STEP_NAME="${RALPH_TOOLING_FAILED_STEP:-}"
TOOLING_FAILED_OUTPUT="${RALPH_TOOLING_FAILED_TAIL:-}"
unset RALPH_TOOLING_COMMIT RALPH_TOOLING_KIND RALPH_TOOLING_MOUNT \
      RALPH_TOOLING_FAILED RALPH_TOOLING_FAILED_STEP RALPH_TOOLING_FAILED_TAIL

# Read the configured base branch from .ralph.yml (default 'main').
# Called once per tick after config validation; cached in BASE_BRANCH.
read_base_branch() {
  python3 -c "
import sys; sys.path.insert(0, '$(dirname "$RALPH_BIN")/../lib')
import ralph_config
r = ralph_config.load_and_validate('$RALPH_CONFIG')
print(r.config['branching']['base'] if r.ok else 'main')
"
}

# Resolve the selected Story's working branch and what it is cut from (#121),
# with the same CLI the iterate prompt uses, so the names cannot drift. Sets
# STORY_BRANCH and STORY_CUT_FROM; returns non-zero when either cannot be named
# (the Story or its PRD cannot be fetched), and the caller then leaves git alone.
STORY_BRANCH="" STORY_CUT_FROM=""
resolve_story_branch() {
  local issue="$1" story_file prd_file="" parent rc=0
  STORY_BRANCH="" STORY_CUT_FROM=""
  story_file="$(mktemp)"
  if ! gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    rm -f "$story_file"
    return 1
  fi
  parent="$(grep -oE 'Parent:[[:space:]]*#[0-9]+' "$story_file" \
    | head -n1 | grep -oE '[0-9]+' || true)"
  if [[ -n "$parent" ]]; then
    prd_file="$(mktemp)"
    gh issue view "$parent" --json number,title,labels,body,state >"$prd_file" 2>/dev/null || rc=1
  fi
  if (( rc == 0 )); then
    STORY_BRANCH="$("$RALPH_BIN" --branch-name "$story_file" "$RALPH_CONFIG" ${prd_file:+"$prd_file"})" \
      && STORY_CUT_FROM="$("$RALPH_BIN" --branch-name "$story_file" "$RALPH_CONFIG" ${prd_file:+"$prd_file"} --base)" \
      || rc=1
  fi
  rm -f "$story_file" ${prd_file:+"$prd_file"}
  return "$rc"
}

# NUL-delimited porcelain preserves worktree paths containing whitespace or
# git's quoting characters. Never take a branch away from another worktree.
story_branch_worktree() {
  local branch="$1" field path="" here
  here="$(pwd -P)"
  while IFS= read -r -d '' field; do
    case "$field" in
      worktree\ *) path="${field#worktree }" ;;
      "branch refs/heads/$branch")
        if [[ "$path" != "$here" ]]; then
          printf '%s\n' "$path"
          return 0
        fi
        ;;
    esac
  done < <(git worktree list --porcelain -z)
  return 1
}

# Put the checkout on the Story's own branch before anything else touches git
# (#121). The tick used to sync and merge into whatever HEAD the previous
# iteration left behind -- another Story's branch, of another Feature -- and a
# conflict there left markers in the .ralph.yml every later tick validates.
#   - origin has the branch: check it out hard-synced to origin, so a human
#     rewrite wins (ADR-0006) -- unless the local branch is origin plus commits
#     nobody pushed yet, which are kept;
#   - only the local branch exists: continue on it;
#   - neither: cut it from origin/<what it is cut from>, falling back to
#     origin/<base> while a Feature branch does not exist yet.
# Uncommitted changes to tracked files are discarded, as the hard sync always
# did, but named first. Returns non-zero when the checkout could not be moved.
checkout_story_branch() {
  local branch="$1" cut_from="$2" base="$3" dirty from
  git fetch -q origin || log "could not fetch origin; working from the local refs"
  dirty="$(git status --porcelain --untracked-files=no)"
  if [[ -n "$dirty" ]]; then
    log "discarding uncommitted changes left in the checkout: $(cut -c4- <<<"$dirty" | paste -sd' ' -)"
    git reset -q --hard || return 1
  fi
  if git rev-parse -q --verify "refs/remotes/origin/$branch" >/dev/null; then
    if git rev-parse -q --verify "refs/heads/$branch" >/dev/null \
       && git merge-base --is-ancestor "origin/$branch" "refs/heads/$branch"; then
      git checkout -q "$branch" || return 1
    else
      git checkout -q --no-track -B "$branch" "origin/$branch" || return 1
    fi
  elif git rev-parse -q --verify "refs/heads/$branch" >/dev/null; then
    log "origin has no $branch; continuing from the local branch"
    git checkout -q "$branch" || return 1
  else
    from="origin/$cut_from"
    git rev-parse -q --verify "refs/remotes/$from" >/dev/null || from="origin/$base"
    log "creating $branch from $from"
    git checkout -q --no-track -b "$branch" "$from" || return 1
  fi
}

# Freshness merge (ADR-0006): a Feature story with out-of-Feature deps (closed
# Orphan Stories or PRDs whose code landed in the base branch after the feature
# branch forked) gets origin/<base> merged into its branch -- the Story branch,
# cut from the Feature tip -- before the iteration. A merge, never a rebase, so
# bench anchors survive. Skipped when the branch already contains the base: the
# decision is graph-only, and some graphs answer yes for every Story of a
# Feature. A conflict is aborted, never left in the tree; the conflicted paths
# are left in FRESHNESS_CONFLICTS and the function returns non-zero.
FRESHNESS_CONFLICTS=""
freshness_merge() {
  local issue="$1" base="$2"
  FRESHNESS_CONFLICTS=""
  if git merge-base --is-ancestor "origin/$base" HEAD; then
    log "freshness merge: $STORY_BRANCH already contains origin/$base; nothing to merge for #$issue"
    return 0
  fi
  log "freshness merge: merging origin/$base into $STORY_BRANCH for #$issue"
  git merge -q --no-edit "origin/$base" && return 0
  FRESHNESS_CONFLICTS="$(git diff --name-only --diff-filter=U | paste -sd' ' -)"
  git merge --abort 2>/dev/null || git reset -q --hard
  log "freshness merge for #$issue conflicts in: ${FRESHNESS_CONFLICTS:-(no paths reported)}; aborted"
  return 1
}

# The Slot this tick runs in (ADR-0008, #134): its number, the per-Slot tick
# lock in the clone's git common directory, and the worktree's path. Resolved
# from the checkout the scheduler launched the tick in -- the lock has to be
# taken before the Slot is touched -- and only by the pre-freeze tick: the
# frozen copy inherits the held lock and the Slot as its working directory.
SLOT_NUMBER="" SLOT_LOCK="" SLOT_PATH="" SLOT_BASE=""
resolve_slot() {
  local line
  if ! line="$("$RALPH_BIN" --slot)" || [[ -z "$line" ]]; then
    log "cannot resolve this tick's slot; refusing to tick"
    return 2
  fi
  read -r SLOT_NUMBER SLOT_LOCK SLOT_PATH <<<"$line"
}

# Provision or repair the Slot and move into it: every checkout, sync and merge
# this tick makes happens there, so the operator keeps their own checkout and a
# half-finished merge of theirs can no longer stop the Loop. A Slot that has to
# be created is cut from origin/<base> when that resolves, so a first tick never
# inherits whichever branch the operator left their checkout on. The base is
# read from their config here because the Slot has none yet; a config too broken
# to read falls back to 'main', and the Slot's own config is validated by
# preflight a moment later.
enter_slot() {
  local what from=""
  # The operator's config, read before the cd: the Slot has none of its own yet,
  # and the one it gets is validated by preflight a moment later. Cached in
  # SLOT_BASE so `sync_slot` does not read a config from inside the Slot -- the
  # very thing it is about to replace.
  SLOT_BASE="$(read_base_branch)"
  if git rev-parse -q --verify "refs/remotes/origin/$SLOT_BASE" >/dev/null 2>&1; then
    from="origin/$SLOT_BASE"
  fi
  if ! what="$("$RALPH_BIN" --slot-ensure "$SLOT_PATH" ${from:+"$from"})"; then
    log "cannot provision slot $SLOT_NUMBER at $SLOT_PATH; refusing to tick"
    return 2
  fi
  log "slot $SLOT_NUMBER: $what $SLOT_PATH"
  if ! cd "$SLOT_PATH"; then
    log "cannot enter slot $SLOT_NUMBER at $SLOT_PATH; refusing to tick"
    return 2
  fi
}

# Start the tick from the committed base (ADR-0008, #134). Two things, in order:
# let go of any branch the Slot still holds -- a tick that crashed mid-Story
# would otherwise keep that branch checked out and no other Slot could ever take
# it -- and hard-reset the detached Slot to origin/<base>. The reset is what
# makes the config, schema and gating this tick validates the ones the base
# commits, rather than whatever a Story left in the Slot ticks ago: a Slot
# created once and only reused would otherwise read the same stale `.ralph.yml`
# for ever, while frozen tooling (ADR-0009) ran origin/<base>'s code against it.
# Nothing is lost by the reset -- a Story's work lives on its own pushed branch,
# and a Slot is never the operator's checkout. Run after the mid-operation
# refusal, so a Slot left mid-merge is reported, not steamrolled.
sync_slot() {
  local branch
  branch="$(git symbolic-ref -q --short HEAD 2>/dev/null)" || branch=""
  if [[ -n "$branch" ]]; then
    if ! git checkout -q --detach; then
      log "could not detach the slot from $branch; refusing to tick"
      return 1
    fi
    log "slot $SLOT_NUMBER: detached, letting go of $branch"
  fi
  # Best-effort: a tick with no network still runs, from the refs it has.
  git fetch -q origin 2>/dev/null \
    || log "could not fetch origin; the slot starts from the refs it has"
  git rev-parse -q --verify "refs/remotes/origin/${SLOT_BASE:-main}" >/dev/null 2>&1 \
    || return 0
  if ! git reset -q --hard "origin/${SLOT_BASE:-main}"; then
    log "could not reset the slot to origin/${SLOT_BASE:-main}; refusing to tick"
    return 1
  fi
}

# Refuse to tick on a Slot that an earlier run left mid-operation (#121): a
# conflicted .ralph.yml would otherwise surface as an unrelated INVALID CONFIG,
# on every tick, until a human looked. It reads the *per-worktree* git dir, so
# the operator's own half-finished merge is none of its business (#134).
checkout_mid_operation() {
  local git_dir
  git_dir="$(git rev-parse --git-dir 2>/dev/null)" || return 1
  [[ -n "$git_dir" ]] || return 1
  if [[ -e "$git_dir/MERGE_HEAD" ]]; then echo "mid-merge"
  elif [[ -d "$git_dir/rebase-merge" || -d "$git_dir/rebase-apply" ]]; then echo "mid-rebase"
  elif [[ -e "$git_dir/CHERRY_PICK_HEAD" ]]; then echo "mid-cherry-pick"
  elif [[ -n "$(git ls-files -u 2>/dev/null)" ]]; then echo "left with unmerged paths"
  else return 1
  fi
}

# Launch one fresh-context Implementation Agent iteration for the selected story.
# `ralph --launch-agent` resolves the configured implementation Model Profile,
# runs it in a fresh process, and reports the outcome as its exit code -- so the
# provider is named nowhere in this script. Returns:
#   RC_SESSION_LIMIT (10) when the agent signalled session-limit exhaustion (the
#                         adapter's verdict, from lib/ralph_session.py) -- the
#                         story gets checkpointed;
#   RC_STORY_COMPLETE (11) when the iteration emitted the done-signal marker,
#                         meaning the gate is green and every acceptance criterion
#                         is checked -- the story gets promoted;
#   RC_INFRA_FAILURE (12) when the launch itself failed (crash, missing CLI) --
#                         no progress to promote; the story is resumed later;
#   0                     otherwise (partial progress -- resume it next pass).
# Session-limit takes priority: a truncated run never counts as complete. These
# codes are the shared exit-code contract in lib/ralph_agent.py (EXIT_*).
RC_SESSION_LIMIT=10
RC_STORY_COMPLETE=11
RC_INFRA_FAILURE=12
# Not part of that contract: the launcher never got as far as running an agent,
# so the same call will fail identically on the next pass. This one ends the tick.
RC_LAUNCH_UNAVAILABLE=13
# Neither: the iteration ended on the needs-human signal (#114). NEEDS_HUMAN
# holds what it said the human must do.
RC_NEEDS_HUMAN=20
NEEDS_HUMAN=""
# A fenced `ralph` call refused because this Slot no longer holds the issue's
# Claim (#139, ADR-0008): another Slot took the lease over and is working it.
# Reported by every mutating subcommand, and by the agent's own calls, which
# inherit RALPH_SLOT. Not a failure of the Story: `claim_lost` below ends the
# unit of work without an Attempt. Keep in step with ralph_claim.EXIT_CLAIM_LOST.
RC_CLAIM_LOST=21
run_iteration() {
  local action="$1" issue="$2" out rc
  export RALPH_ITERATION_ACTION="$action" RALPH_ITERATION_ISSUE="$issue"
  local prompt
  if [[ -f "$ITERATE_PROMPT" ]]; then
    prompt="$(cat "$ITERATE_PROMPT")"
  else
    prompt="Implement the next Ralph story test-first to a green local gate."
  fi
  prompt+=$'\n\n---\nNext action: '"$action #$issue"$'. Work only this story this iteration.\n'

  # Hand the story to the launcher so its recorded model assignment (#46) picks
  # the implementation model: a resume or a retry runs the model the story was
  # assigned, never whatever the config now defaults to. Best-effort -- if the
  # story cannot be fetched the launcher falls back to the committed defaults.
  local story_file
  story_file="$(mktemp)"
  local story_arg=()
  if gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    story_arg=(--story "$story_file")
  fi

  set +e
  out="$(printf '%s' "$prompt" | "$RALPH_BIN" --launch-agent implementation "$RALPH_CONFIG" "${story_arg[@]}" 2>&1)"
  rc=$?
  set -e
  rm -f "$story_file"
  [[ -n "$out" ]] && printf '%s\n' "$out"

  # Session-limit takes priority over the done-signal: a truncated run is never
  # complete. The adapter already classified the run through lib/ralph_session.py
  # and returned RC_SESSION_LIMIT (10) on exhaustion, so the tick just reads it.
  if [[ "$rc" -eq "$RC_SESSION_LIMIT" ]]; then
    return "$RC_SESSION_LIMIT"
  fi
  # Waiting on a human outranks the done-signal: an iteration that says both
  # is unsure, and a human looking costs less than a promotion nobody checked.
  NEEDS_HUMAN="$(printf '%s\n' "$out" | grep -F -- "$RALPH_NEEDS_HUMAN_MARKER" | tail -n1 || true)"
  if [[ -n "$NEEDS_HUMAN" ]]; then
    NEEDS_HUMAN="${NEEDS_HUMAN#*"$RALPH_NEEDS_HUMAN_MARKER"}"
    NEEDS_HUMAN="${NEEDS_HUMAN#"${NEEDS_HUMAN%%[![:space:]]*}"}"
    return "$RC_NEEDS_HUMAN"
  fi
  if printf '%s' "$out" | grep -qF "$RALPH_STORY_COMPLETE_MARKER"; then
    return "$RC_STORY_COMPLETE"
  fi
  if [[ "$rc" -eq "$RC_INFRA_FAILURE" ]]; then
    return "$RC_INFRA_FAILURE"
  fi
  # Only a clean exit is partial progress. Any other code means the launcher
  # itself refused -- a bad config, a refused role resolution, or a `ralph` that
  # does not know the subcommand (exit 2) -- so no agent ran at all. That is
  # distinct from RC_INFRA_FAILURE, where the provider did start and may have
  # done work before dying. Reporting it as progress re-selects the same story
  # every pass and spins the tick to its iteration bound having launched nothing,
  # which is how a 13-minute no-op tick still exited 0 (observed 2026-08-28).
  if (( rc != 0 )); then
    log "the launcher refused to run the agent for #$issue (exit $rc)"
    return "$RC_LAUNCH_UNAVAILABLE"
  fi
  return 0
}

# How many Handoffs the story carries, or nothing when it cannot be read.
story_handoffs() {
  gh issue view "$1" --json comments 2>/dev/null \
    | "$RALPH_BIN" --count-handoffs - 2>/dev/null || true
}

# Record a failed Attempt (ADR-0004); at limits.max_attempts the story moves to
# state:blocked, so resume-first stops selecting it. Best-effort: if it cannot
# be recorded, the iteration bound is still there. The breaker is asked every
# time: a Story blocked here counts toward limits.circuit_breaker, and without
# asking, the tick went straight on to block the next Story too.
# A Claim lost here (#139) is the one failure that must not be recorded: the
# Attempt would be charged to a Story another Slot is working, and the breaker
# would count a block this Slot has no business declaring. Returns
# RC_CLAIM_LOST so the caller ends the unit of work instead.
record_failed_attempt() {
  local issue="$1" reason="$2" rc=0
  log "#$issue: $reason; recording a failed Attempt"
  gh issue view "$issue" --json number,title,labels,body,state,comments \
    | "$RALPH_BIN" --record-attempt - "$reason" "$RALPH_CONFIG" || rc=$?
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return 0; fi
  if (( rc != 0 )); then
    log "could not record the Attempt for #$issue; continuing"
  fi
  check_breaker
}

# Write a Handoff for the current in-progress story and end the tick cleanly.
# The story is fetched fresh from gh so `ralph --checkpoint` has the full record.
checkpoint_story() {
  local issue="$1" rc=0
  log "session limit reached; checkpointing #$issue via Handoff"
  gh issue view "$issue" \
     --json number,title,labels,body,comments,state \
   | "$RALPH_BIN" --checkpoint - "Session limit reached; resume next tick." "$RALPH_CONFIG" \
   || rc=$?
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return 0; fi
  return "$rc"
}

# Park the story on a human (#114): it said it can only go on once a human acts.
# No Attempt is spent and the story is never state:blocked, so one Story waiting
# correctly on a human can no longer trip the circuit breaker and halt the loop.
# Selection skips it from here on; removing needs-human resumes it.
park_story() {
  local issue="$1" need="$2" rc=0
  log "#$issue is waiting on a human; parking it on needs-human"
  gh issue view "$issue" --json number,title,labels,body,state \
    | "$RALPH_BIN" --park-story - "$need" "$RALPH_CONFIG" || rc=$?
  # A Claim lost here (#139) is not a parking failure: the new holder decides
  # what this issue needs now, and the unit of work is simply over.
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return 0; fi
  return "$rc"
}

# Count a Handoff that left the Story branch unchanged (#111); at the limit the
# Story is parked on needs-human, quoting its latest Handoff. Best-effort: an
# uncounted idle Handoff only delays the parking.
record_idle_handoff() {
  local issue="$1"
  log "#$issue handed off without changing $STORY_BRANCH; counting an idle Handoff"
  local rc=0
  gh issue view "$issue" --json number,title,labels,body,state,comments \
    | "$RALPH_BIN" --record-idle-handoff - "$STORY_BRANCH" "$RALPH_CONFIG" || rc=$?
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return 0; fi
  if (( rc != 0 )); then
    log "could not record the idle Handoff for #$issue; continuing"
  fi
}

# Move a freshly-selected story from state:ready to state:in-progress before its
# first iteration. This is the `start` edge of the state machine that the rest of
# the loop assumes exists: a mid-iteration checkpoint then resumes it (resume-first
# needs state:in-progress), a partial pass is re-selected as `resume` not `start`,
# and HIL/AFK completion's `--remove-label state:in-progress` has a label to move.
# Best-effort: a failure here almost always means the labels were never created,
# so point at `ralph --init` and continue (the iteration can still do work).
begin_story() {
  local issue="$1" rc=0
  # Fenced before the label move (#139, F-3 of PR #160 round 1): this is the
  # tick's own `gh` write, not a subcommand's, so the dispatcher's fence never
  # sees it -- and it is the *first* write of a `start`, before every fenced
  # subcommand of the unit. A Slot whose Claim was taken over between
  # acquiring it and here must leave the new holder's labels alone.
  "$RALPH_BIN" --fence-claim "$issue" "$RALPH_CONFIG" || rc=$?
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return "$RC_CLAIM_LOST"; fi
  log "moving #$issue to state:in-progress"
  gh issue edit "$issue" \
     --add-label state:in-progress --remove-label state:ready \
   || log "could not label #$issue state:in-progress (are the Ralph labels created? run 'ralph --init'); continuing"
}

# Record the story's implementation/review model assignment durably before its
# iteration (#46). Resolves each role the story does not already carry a label
# for and applies `model:impl:<id>` / `model:review:<id>`, so later retries,
# resumes and audits read the assignment from the backlog rather than from
# whatever configuration is current. Idempotent: a story that is already
# assigned is left untouched, which is also why this runs on `resume` -- a story
# started before it had an assignment heals forward instead of staying blank.
# Best-effort: a failure here does not stop the iteration (the launcher still
# falls back to the committed defaults).
assign_story_models() {
  local issue="$1" story_file rc=0
  story_file="$(mktemp)"
  if ! gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    log "cannot fetch #$issue to record its model assignment; continuing"
    rm -f "$story_file"
    return 0
  fi
  "$RALPH_BIN" --assign-models "$story_file" "$RALPH_CONFIG" >/dev/null || rc=$?
  rm -f "$story_file"
  # Fenced (#139): this is the first write of the unit, so a Claim already
  # lost here means another Slot owns the Story and nothing else should run.
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return "$RC_CLAIM_LOST"; fi
  if (( rc != 0 )); then
    log "could not record the model assignment for #$issue (exit $rc); continuing"
  fi
  return 0
}

# Wait out one bounded review window for a Story In Review (#54, and the
# Negotiation Rounds of #53 that happen inside it). Waiting belongs to the
# orchestration process, not to a model: `ralph --await-review` polls durable
# GitHub state with backing-off intervals and launches the assigned Review
# Agent only when the pull-request head has no review yet, so idle GitHub time
# spends no context and no tokens. This tick holds its lock throughout, which
# is what keeps a Story in negotiation from competing with new work.
# Returns RC_REVIEW_WINDOW (14) when the window closed: the caller owes a
# Handoff. RC_REVIEW_ESCALATED (15) when the round budget ran out with the two
# models still disagreeing: that Story is now state:blocked with a human asked
# to arbitrate, and the tick moves on to other work. Any other non-zero leaves
# the Story In Review for the next tick.
RC_REVIEW_WINDOW=14
RC_REVIEW_ESCALATED=15
RC_REVIEW_COMPLETED=16
RC_REVIEW_BLOCKED=19
await_review() {
  local issue="$1" story_file rc=0
  story_file="$(mktemp)"
  if ! gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    log "cannot fetch #$issue for its review round; leaving it for the next tick"
    rm -f "$story_file"
    return 0
  fi
  "$RALPH_BIN" --await-review "$story_file" "$RALPH_CONFIG" || rc=$?
  rm -f "$story_file"
  return "$rc"
}

# Checkpoint a Story In Review when its review window closes. The Handoff is
# **comment-only**: the Story's work is already pushed, and a commit -- even the
# empty one a normal checkpoint writes -- would move the head the reviewer's
# findings are bound to, discarding the review this tick just waited for.
# The Story stays state:in-review, so the next tick resumes it (and its pull
# request) ahead of any state:ready work.
checkpoint_review() {
  local issue="$1" story_file rc=0
  story_file="$(mktemp)"
  if ! gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    log "cannot fetch #$issue to write its review Handoff; continuing"
    rm -f "$story_file"
    return 0
  fi
  "$RALPH_BIN" --checkpoint "$story_file" \
    "Review window closed with the negotiation still open. The Story stays In Review; the next tick resumes it and its pull request first." \
    "$RALPH_CONFIG" --comment-only || rc=$?
  rm -f "$story_file"
  if (( rc == RC_CLAIM_LOST )); then claim_lost "$issue"; return 0; fi
  if (( rc != 0 )); then
    log "could not write the review Handoff for #$issue (exit $rc); continuing"
  fi
  return 0
}

# Ask the circuit breaker whether the loop as a whole should stop. A deadlocked
# Story blocks itself and nothing else -- but "blocked" is exactly what the
# breaker counts, so a *pattern* of deadlocks still halts the loop and tags the
# configured human. The escalation never decides that; this does, from the
# backlog, using the same limits.circuit_breaker the failure path uses.
# Best-effort: a gh blip here must not turn one blocked Story into a failed tick.
check_breaker() {
  "$RALPH_BIN" --check-breaker \
    || log "could not evaluate the circuit breaker; continuing"
}

# Promote locally green implementation work into review. Both Story types use
# the same marked-PR boundary and move to state:in-review; final AFK/HIL
# completion is a later, review-gated stage. The tool validates
# the Story type/state, so this stays thin orchestration.
# The story is fetched fresh so promotion has the full record. A Feature story
# (Parent: #N, ADR-0006) needs its PRD issue to resolve the feature branch, so
# the PRD is fetched and handed to the promotion CLI as a temp file; an Orphan
# Story (Parent: None) passes no PRD and keeps the classic path. Returns
# non-zero on a git/gh/dispatch failure; the caller logs and moves on.
complete_story() {
  local issue="$1" story_json parent prd_file="" rc=0
  story_json="$(gh issue view "$issue" --json number,title,labels,body,state)"
  parent="$(grep -oE 'Parent:[[:space:]]*#[0-9]+' <<<"$story_json" \
    | head -n1 | grep -oE '[0-9]+' || true)"
  if [[ -n "$parent" ]]; then
    prd_file="$(mktemp)"
    if ! gh issue view "$parent" --json number,title,labels,body,state >"$prd_file"; then
      log "cannot fetch PRD #$parent for Feature story #$issue"
      rm -f "$prd_file"
      return 2
    fi
  fi
  if grep -qE '"type:(afk|hil)"' <<<"$story_json"; then
    log "implementation green #$issue; entering review (--implementation-green)"
    "$RALPH_BIN" --implementation-green - "$RALPH_CONFIG" ${prd_file:+"$prd_file"} \
      <<<"$story_json" || rc=$?
  else
    log "cannot promote #$issue: no type:afk/type:hil label"
    rc=2
  fi
  if [[ -n "$prd_file" ]]; then rm -f "$prd_file"; fi
  return "$rc"
}

# Act on any human arbitration that arrived since the last tick (#58).
#
# A Story that deadlocked is state:blocked, and a blocked Story is by definition
# never selected -- so the work loop above can never notice that a human has
# since clicked Approve or Request changes on its pull request. This pass is
# where that is noticed: it scans the blocked Stories and hands each one to
# `ralph --arbitrate-review`, which does nothing at all unless there is a native
# review it has not already acted on.
#
# It runs at the end of the tick, next to the Feature completion pass, because
# it is reconciliation with what happened *between* ticks rather than part of
# working the backlog. A human's Request changes still gets its implementation
# round here and now; only the review of that new head waits for the next tick.
# A human deciding while the Story is still In Review needs none of this -- the
# bounded wait sees the decision on its next poll and acts on it immediately.
arbitration_pass() {
  local issue
  while IFS= read -r issue; do
    [[ -z "$issue" ]] && continue
    reconcile_with_claim "$issue" arbitrate_story "$issue" \
      || log "could not act on a human decision for #$issue; the next tick retries"
  done < <("$RALPH_BIN" --blocked-stories 2>/dev/null)
}

arbitrate_story() {
  local issue="$1" story_file rc=0
  story_file="$(mktemp)"
  if gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null; then
    fence_reconciliation_claim &&
      "$RALPH_BIN" --arbitrate-review "$story_file" "$RALPH_CONFIG" || rc=$?
  else
    log "cannot fetch blocked #$issue to check for a human decision; continuing"
    rc=1
  fi
  rm -f "$story_file"
  return "$rc"
}

# Run the Feature completion pass for a single eligible PRD (ADR-0006, US-029).
# Fetches the PRD issue, then delegates to `ralph --complete-feature`.
complete_feature() {
  local prd_number="$1" prd_file rc=0
  prd_file="$(mktemp)"
  if ! gh issue view "$prd_number" --json number,title,labels,body,state >"$prd_file"; then
    log "cannot fetch PRD #$prd_number for completion pass"
    rm -f "$prd_file"
    return 1
  fi
  # The scan happened before acquisition: a previous holder may have closed
  # or blocked this PRD before we obtained its Claim. Re-read under the lease.
  if ! python3 - "$prd_file" <<'PY_ELIGIBLE'
import json, sys
with open(sys.argv[1]) as source:
    prd = json.load(source)
labels = [label['name'] if isinstance(label, dict) else label
          for label in prd.get('labels', [])]
sys.exit(0 if prd.get('state', '').upper() == 'OPEN'
         and 'state:ready' in labels and 'ready-for-human' not in labels else 1)
PY_ELIGIBLE
  then
    log "PRD #$prd_number is no longer eligible; skipping completion"
    rm -f "$prd_file"
    return 0
  fi
  log "eligible PRD #$prd_number; running completion pass (--complete-feature)"
  fence_reconciliation_claim &&
    "$RALPH_BIN" --complete-feature "$prd_file" "$RALPH_CONFIG" || rc=$?
  rm -f "$prd_file"
  return "$rc"
}

# The tick's preflight: everything that must hold before anything is labelled.
# Sets PREFLIGHT_STEP to the step that failed. The pre-freeze tick runs it
# against a candidate copy (`ralph.sh --preflight`, which adds a dry run).
PREFLIGHT_STEP=""
preflight() {
  PREFLIGHT_STEP=""
  # --- config validation at tick start (ADR-0001: fail loud) ---
  if ! "$RALPH_BIN" --check-config "$RALPH_CONFIG" >/dev/null; then
    log "invalid or missing $RALPH_CONFIG; refusing to tick"
    PREFLIGHT_STEP="config check"
    return 2
  fi

  # --- the tick's own tooling must be complete (ADR-0001: fail loud) ---
  # `$RALPH_BIN` is re-read from disk on every call, and a tick checks out
  # branches -- so it can end up pointing at a `ralph` older than this script.
  # That happened on 2026-08-28: promoting a story moved the checkout to a branch
  # without `--launch-agent`, and every later iteration launched nothing. Checked
  # here, before any story is labelled state:in-progress, so a mismatch cannot
  # leave the backlog half-edited the way that tick did.
  local sub usage missing=()
  usage="$("$RALPH_BIN" --help 2>&1 || true)"
  for sub in --dry-run --claim --renew-claim --heartbeat-claim --release-claim --verify-claim --claim-holder --fence-claim --claimed-issues --feature-claim --check-pointer --launch-agent --assign-models --checkpoint --count-handoffs --record-attempt --implementation-green --review-round --await-review --escalate-review --arbitrate-review --complete-story --blocked-stories --check-breaker --park-story --record-idle-handoff --unpark-stories --prune-tooling --fallback-issue; do
    grep -qF -- "$sub" <<<"$usage" || missing+=("$sub")
  done
  if (( ${#missing[@]} )); then
    log "$RALPH_BIN does not support: ${missing[*]}"
    log "the checkout providing bin/ralph is out of step with this bin/ralph.sh; refusing to tick"
    PREFLIGHT_STEP="subcommand preflight"
    return 2
  fi

  # --- the ai-utils that runs is the one the Superproject commits (#96) ---
  # Before anything is labelled or launched: a Superproject that pins one
  # ai-utils while another runs debugs the wrong version (autopilot_controller
  # pinned 8eba071 while 744b8b3 ran). A deliberate mismatch is allowed by
  # config, and logged by the check itself.
  # Frozen, the copy is told which commit and mount it stands for: its own path
  # sits in a git directory, not at the mount.
  if ! RALPH_TOOLING_COMMIT="$TOOLING_COMMIT" RALPH_TOOLING_MOUNT="$TOOLING_MOUNT" \
       "$RALPH_BIN" --check-pointer "$RALPH_CONFIG"; then
    log "the running ai-utils is not the one the Superproject commits; refusing to tick"
    PREFLIGHT_STEP="pointer check"
    return 2
  fi
}

# Run the tooling a candidate copy holds through the tick's preflight, plus a dry
# run, without taking the lock or touching the backlog. Prints the failed step
# on a RALPH-PREFLIGHT-FAILED line.
preflight_only() {
  local rc=0 action
  preflight || rc=$?
  if (( rc == 0 )); then
    if ! action="$("$RALPH_BIN" --dry-run)"; then
      PREFLIGHT_STEP="dry run" rc=2
    else
      case "${action%% *}" in
        no-work|halt|resume|start) ;;
        *) log "unrecognized action from --dry-run: $action"
           PREFLIGHT_STEP="dry run" rc=2 ;;
      esac
    fi
  fi
  (( rc == 0 )) || printf 'RALPH-PREFLIGHT-FAILED: %s\n' "$PREFLIGHT_STEP"
  return "$rc"
}

# Preflight COPY as the frozen tooling for COMMIT. On failure TOOLING_FAILED_STEP
# names the step and TOOLING_FAILED_TAIL holds the end of its output.
TOOLING_FAILED_STEP="" TOOLING_FAILED_TAIL=""
preflight_copy() {
  local copy="$1" commit="$2" kind="$3" mount="$4" out rc=0
  TOOLING_FAILED_STEP="" TOOLING_FAILED_TAIL=""
  out="$(env -u RALPH_CLI RALPH_TOOLING_COMMIT="$commit" RALPH_TOOLING_KIND="$kind" \
           RALPH_TOOLING_MOUNT="$mount" "$copy/bin/ralph.sh" --preflight 2>&1)" || rc=$?
  (( rc == 0 )) && return 0
  TOOLING_FAILED_STEP="$(sed -n 's/^RALPH-PREFLIGHT-FAILED: //p' <<<"$out" | tail -n1)"
  # No step named: the copy's tick script itself crashed before it could say.
  : "${TOOLING_FAILED_STEP:=the tick script (exit $rc)}"
  TOOLING_FAILED_TAIL="$(grep -v '^RALPH-PREFLIGHT-FAILED: ' <<<"$out" | tail -n 20)"
  return "$rc"
}

# Hold COMMIT's copy for as long as this tick runs from it (#147): fd 8 is its
# lock, shared, kept across the exec. Pruning takes it exclusively, so a copy in
# use is never removed, and a copy being removed is waited for, then recreated.
hold_copy() {
  local copies="$1" commit="$2"
  mkdir -p "$copies" || return 1
  exec 8>"$copies/$commit.lock" || return 1
  flock -s -w 120 8
}

# Tell the operator about a fallback through one issue, and close it once main
# passes again (#149). Best-effort: GitHub being down must not stop the tick
# that is keeping the Loop alive. `fallback-open` remembers that an issue may be
# open, so a healthy tick asks GitHub nothing; it goes once the close worked.
report_fallback() {
  local copies="$1" flag="$1/fallback-open" tail_file
  [[ "$TOOLING_KIND" == self-hosted ]] || return 0
  if [[ -n "$TOOLING_FAILED" ]]; then
    : >"$flag" || true
    tail_file="$(mktemp)"
    printf '%s\n' "$TOOLING_FAILED_OUTPUT" >"$tail_file"
    "$RALPH_BIN" --fallback-issue fallback "$TOOLING_FAILED" "$TOOLING_FAILED_STEP_NAME" \
      "$TOOLING_COMMIT" "$tail_file" "$RALPH_CONFIG" \
      || log "could not report the fallback on its issue; continuing"
    rm -f "$tail_file"
  elif [[ -e "$flag" ]]; then
    if "$RALPH_BIN" --fallback-issue recovered "$TOOLING_COMMIT" "$RALPH_CONFIG"; then
      rm -f "$flag"
    else
      log "could not close the fallback issue; the next tick retries"
    fi
  fi
}

# Lock and make COMMIT's copy (fd 8, see hold_copy); sets COPY.
COPY=""
prepare_copy() {
  local commit="$1" repo="$2" copies="$3"
  COPY=""
  if ! hold_copy "$copies" "$commit"; then
    log "cannot lock the frozen copy of $commit; refusing to tick"
    return 2
  fi
  if ! COPY="$("$RALPH_BIN" --tooling-copy "$commit" "$repo")"; then
    log "cannot make the frozen copy of $commit; refusing to tick"
    return 2
  fi
}

# Freeze the tooling (ADR-0009): resolve the Tooling Commit, make sure its copy
# exists, preflight it, and exec the copy's tick -- once, with the lock (fd 9)
# still held, because the exec keeps the open file. Returns 0 to tick unfrozen
# (ai-utils is installed outside the checkout, or drift is allowed), non-zero
# to refuse; on success it never returns.
#
# Self-hosted, a commit that passes preflight becomes Last-Known-Good (#148),
# and a new origin/<base> that fails runs Last-Known-Good instead: one bad merge
# must not brick the Loop that would fix it. The failing commit is tried again
# on every tick. A Superproject never falls back: its pin is the contract, so a
# failing pin is refused, naming it.
freeze_tooling() {
  local line kind commit repo mount copies lkg
  line="$("$RALPH_BIN" --tooling-commit "$RALPH_CONFIG")" || {
    log "cannot resolve the Tooling Commit; refusing to tick"
    return 2
  }
  [[ "$line" == unfrozen ]] && return 0
  read -r kind commit repo mount copies <<<"$line"
  [[ "$mount" == - ]] && mount=""
  prepare_copy "$commit" "$repo" "$copies" || return $?
  if preflight_copy "$COPY" "$commit" "$kind" "$mount"; then
    if [[ "$kind" == self-hosted ]]; then
      printf '%s\n' "$commit" >"$copies/last-known-good.tmp" \
        && mv -f "$copies/last-known-good.tmp" "$copies/last-known-good" \
        || log "could not record $commit as Last-Known-Good; continuing"
    fi
    export RALPH_TOOLING_COMMIT="$commit" RALPH_TOOLING_KIND="$kind" RALPH_TOOLING_MOUNT="$mount"
    unset RALPH_CLI
    exec "$COPY/bin/ralph.sh" "$@"
  fi
  local step="$TOOLING_FAILED_STEP" tail="$TOOLING_FAILED_TAIL"
  log "the Tooling Commit $commit fails preflight at the $step:"
  printf '%s\n' "$TOOLING_FAILED_TAIL"
  if [[ "$kind" == superproject ]]; then
    log "the ai-utils pinned at $mount ($commit) fails preflight at the $step; refusing to tick"
    return 2
  fi
  lkg="$(cat "$copies/last-known-good" 2>/dev/null || true)"
  if [[ -z "$lkg" || "$lkg" == "$commit" ]]; then
    log "the ai-utils Tooling Commit $commit fails preflight at the $step, and there is no Last-Known-Good to fall back to; refusing to tick"
    return 2
  fi
  prepare_copy "$lkg" "$repo" "$copies" || return $?
  if ! preflight_copy "$COPY" "$lkg" "$kind" "$mount"; then
    printf '%s\n' "$TOOLING_FAILED_TAIL"
    log "the ai-utils Tooling Commit $commit fails preflight at the $step, and Last-Known-Good $lkg fails at the $TOOLING_FAILED_STEP too; refusing to tick"
    return 2
  fi
  log "falling back to Last-Known-Good $lkg: the Tooling Commit $commit fails preflight at the $step"
  export RALPH_TOOLING_COMMIT="$lkg" RALPH_TOOLING_KIND="$kind" RALPH_TOOLING_MOUNT="$mount" \
         RALPH_TOOLING_FAILED="$commit" RALPH_TOOLING_FAILED_STEP="$step" \
         RALPH_TOOLING_FAILED_TAIL="$tail"
  unset RALPH_CLI
  exec "$COPY/bin/ralph.sh" "$@"
}

tick() {
  # --- the Slot and its flock: one tick per Slot, overlapping ticks exit ---
  if [[ -n "$TOOLING_COMMIT" ]] && { true >&9; } 2>/dev/null; then
    # The pre-freeze tick resolved the Slot, took its lock and exec'd this
    # frozen copy from inside it: fd 9 is that lock, still held, and the working
    # directory is already the Slot -- checked for a mid-operation, detached and
    # reset to the base. Re-opening the lock would release it for a moment, and
    # preparing the Slot again would be a second tick's worth of git.
    flock -n 9 || { log "a tick is already running in slot $RALPH_SLOT (lock held); exiting"; return 0; }
  else
    resolve_slot || return $?
    exec 9>"$SLOT_LOCK"
    if ! flock -n 9; then
      log "a tick is already running in slot $SLOT_NUMBER (lock held); exiting"
      return 0
    fi
    enter_slot || return $?
    local mid
    if mid="$(checkout_mid_operation)"; then
      log "the slot at $PWD is $mid; refusing to tick until a human finishes or aborts it"
      return 2
    fi
    sync_slot || return 2
  fi

  # --- frozen tooling: run an immutable copy of the Tooling Commit (ADR-0009) ---
  if [[ -z "$TOOLING_COMMIT" ]]; then
    freeze_tooling "$@" || return $?
  elif [[ -n "$TOOLING_FAILED" ]]; then
    log "tooling: running Last-Known-Good $TOOLING_COMMIT from $(cd "$SCRIPT_DIR/.." && pwd); the Tooling Commit $TOOLING_FAILED fails preflight at the $TOOLING_FAILED_STEP_NAME"
  else
    log "tooling: running the $TOOLING_KIND Tooling Commit $TOOLING_COMMIT from $(cd "$SCRIPT_DIR/.." && pwd)"
  fi

  preflight || return $?

  # --- prune the frozen copies nothing needs any more (#147; best-effort) ---
  if [[ -n "$TOOLING_COMMIT" ]]; then
    local copies
    copies="$(cd "$SCRIPT_DIR/../.." && pwd)"
    # A failing Tooling Commit is still the current one: kept, to be retried.
    "$RALPH_BIN" --prune-tooling "$copies" "$TOOLING_COMMIT" \
      ${TOOLING_FAILED:+"$TOOLING_FAILED"} \
      || log "could not prune the frozen tooling copies; continuing"
    report_fallback "$copies"
  fi

  # --- read the base branch once (for freshness merges) ---
  local base_branch rc=0
  base_branch="$(read_base_branch)"

  # --- story work, then reconciliation, however the story work ends ---
  # The story loop ends early far more often than it runs dry -- a Story left
  # in review, a session limit, a halt -- and the passes below used to follow a
  # clean exit only, so a finished Feature starved behind story work for as
  # long as there was any (#120). The tick's exit code is still the loop's.
  work_backlog "$base_branch" || rc=$?
  completion_pass
  # --- arbitration pass: act on human decisions made between ticks (#58) ---
  arbitration_pass
  # --- unpark pass: a human answered a parked Story between ticks (#111) ---
  "$RALPH_BIN" --unpark-stories 2>/dev/null \
    || log "could not check parked Stories for a human answer; continuing"
  return "$rc"
}

# A Claim is a lease (ADR-0008): it expires unless renewed, so a unit of work
# longer than `concurrency.claim_ttl` keeps its Claim alive with a heartbeat --
# a child process renewing it by compare-and-swap at a fifth of the lease. It
# is started when the Claim is taken and stopped when the unit ends, and it
# watches this Tick, so a Tick that is killed outright takes it with it (#137).
# A unit of work holds up to two Claims -- its issue's (CLAIM_*) and, for a
# Feature Story, its PRD's (FEATURE_CLAIM_*, #141) -- so both functions take
# the prefix of the variables they keep the lease in (default CLAIM).
start_claim_heartbeat() {
  local -n hb_oid="${2:-CLAIM}_OID" hb_file="${2:-CLAIM}_OID_FILE" \
    hb_pid="${2:-CLAIM}_HEARTBEAT"
  hb_file="$(mktemp "${TMPDIR:-/tmp}/ralph-claim-XXXXXX")"
  # $$ is this Tick, which is *not* the heartbeat's parent: story work runs in
  # the `work_backlog` subshell, and that subshell survives a kill of the Tick.
  # A heartbeat watching only its parent would go on renewing from there.
  RALPH_TICK_PID=$$ \
    "$RALPH_BIN" --heartbeat-claim "$1" "$hb_oid" "$RALPH_CONFIG" \
    >"$hb_file" &
  hb_pid=$!
}

stop_claim_heartbeat() {
  local -n hb_oid="${1:-CLAIM}_OID" hb_file="${1:-CLAIM}_OID_FILE" \
    hb_pid="${1:-CLAIM}_HEARTBEAT"
  [[ -z "${hb_pid:-}" ]] && return 0
  # TERM asks the heartbeat to stop at its next safe point and we wait it out:
  # a renewal already in flight has moved the Claim on origin, and killing it
  # before it reports that object id would leave nothing able to release the
  # ref until it expired. A renewal wedged on the network blocks here exactly
  # as any other git push of this tick would.
  kill -TERM "$hb_pid" 2>/dev/null || true
  wait "$hb_pid" 2>/dev/null || true
  # Whatever the heartbeat last swapped to *is* the Claim now: releasing it
  # against the value it was taken with would be refused as stale.
  local renewed
  renewed="$(tail -n 1 "$hb_file" 2>/dev/null || true)"
  [[ -n "$renewed" ]] && hb_oid="$renewed"
  rm -f "$hb_file"
  hb_pid="" hb_file=""
}

# One Slot per Feature (#141, ADR-0008): a Feature Story's unit of work also
# holds its PRD's Claim, unless `concurrency.feature_exclusive` is false. Taken
# after the Story's own Claim; returns 1 when the PRD is held elsewhere (or
# the Story cannot be read to tell), so the caller skips the Story.
claim_story_feature() {
  local issue="$1" story_file prd
  story_file="$(mktemp)"
  if ! gh issue view "$issue" --json number,title,labels,body,state >"$story_file" 2>/dev/null \
     || ! prd="$("$RALPH_BIN" --feature-claim "$story_file" "$RALPH_CONFIG")"; then
    rm -f "$story_file"
    log "cannot tell which Feature #$issue belongs to; skipping it for this tick"
    return 1
  fi
  rm -f "$story_file"
  [[ -z "$prd" ]] && return 0
  if ! FEATURE_CLAIM_OID="$("$RALPH_BIN" --claim "$prd" "$RALPH_CONFIG")"; then
    log "could not claim Feature #$prd for #$issue; skipping it for this tick"
    FEATURE_CLAIM_OID=""
    return 1
  fi
  FEATURE_CLAIM_ISSUE="$prd"
  start_claim_heartbeat "$prd" FEATURE_CLAIM
}

# This Slot's mutation was refused because it no longer holds the issue's
# Claim (#139): its lease lapsed and another Slot took the issue over. The
# unit of work is over, and the right response is to write nothing more --
# whatever the new holder has done to the labels, the comments and the branch
# is now the truth, and a second writer is exactly what the Claim prevents.
# So: no Attempt (this is not the Story failing), nothing toward the circuit
# breaker, and no release -- the Claim is not ours to delete any more, so only
# the heartbeat is stopped and the EXIT trap is left with nothing to do. The
# issue and its current holder are logged, which is the one thing a lost Claim
# is worth saying out loud.
claim_lost() {
  local issue="$1" holder
  holder="$("$RALPH_BIN" --claim-holder "$issue" 2>/dev/null || true)"
  log "Claim lost for #$issue (held by ${holder:-another Slot}); ending the unit of work, recording no Attempt"
  stop_claim_heartbeat
  CLAIM_ISSUE="" CLAIM_OID="" RALPH_CLAIM_ISSUE=""
  # Free the Story branch for its new holder; it is working the issue now.
  git checkout -q --detach || log "could not detach the Slot after #$issue"
}

# Work eligible stories in sequence, resume-first via the engine. Returns what
# the tick should exit with; every early end of the tick is a return from here.
release_story_claim() {
  stop_claim_heartbeat
  stop_claim_heartbeat FEATURE_CLAIM
  [[ -z "${CLAIM_ISSUE:-}" && -z "${FEATURE_CLAIM_ISSUE:-}" ]] && return 0
  # Free the branch before making the Claim available to another Slot.
  if ! git checkout -q --detach; then
    log "could not detach the Slot after #${CLAIM_ISSUE:-$FEATURE_CLAIM_ISSUE}"
    return 2
  fi
  # Each release is attempted whatever became of the other: one that fails
  # must not strand the other Claim until it expires.
  local rc=0
  if [[ -n "${CLAIM_ISSUE:-}" ]]; then
    if "$RALPH_BIN" --release-claim "$CLAIM_ISSUE" "$CLAIM_OID"; then
      log "released Claim for #$CLAIM_ISSUE; Slot detached"
      CLAIM_ISSUE="" CLAIM_OID="" RALPH_CLAIM_ISSUE=""
    else
      log "could not release the Claim for #$CLAIM_ISSUE"
      rc=2
    fi
  fi
  # The Feature's Claim goes last: until it does, no sibling is selectable.
  if [[ -n "${FEATURE_CLAIM_ISSUE:-}" ]]; then
    if "$RALPH_BIN" --release-claim "$FEATURE_CLAIM_ISSUE" "$FEATURE_CLAIM_OID"; then
      log "released Claim for Feature #$FEATURE_CLAIM_ISSUE"
      FEATURE_CLAIM_ISSUE="" FEATURE_CLAIM_OID=""
    else
      log "could not release the Claim for Feature #$FEATURE_CLAIM_ISSUE"
      rc=2
    fi
  fi
  return "$rc"
}

# Stop renewal before this compare-and-swap so it cannot race our last-seen
# object id. Losing the lease refuses the delegation without an Attempt.
fence_reconciliation_claim() {
  stop_claim_heartbeat
  local renewed
  renewed="$("$RALPH_BIN" --renew-claim "$CLAIM_ISSUE" "$CLAIM_OID" "$RALPH_CONFIG")" || return 1
  CLAIM_OID="$renewed"
  start_claim_heartbeat "$CLAIM_ISSUE"
  [[ -z "${FEATURE_CLAIM_ISSUE:-}" ]] && return 0
  stop_claim_heartbeat FEATURE_CLAIM
  renewed="$("$RALPH_BIN" --renew-claim "$FEATURE_CLAIM_ISSUE" "$FEATURE_CLAIM_OID" "$RALPH_CONFIG")" || return 1
  FEATURE_CLAIM_OID="$renewed"
  start_claim_heartbeat "$FEATURE_CLAIM_ISSUE" FEATURE_CLAIM
}

# Reconciliation owns the same lease lifecycle as Story work. Scope the trap
# to one issue, including failures and blockers, and release only after the
# branch is detached so another Slot can immediately resume it.
reconcile_with_claim() (
  local issue="$1"
  shift
  CLAIM_ISSUE="" CLAIM_OID="" CLAIM_HEARTBEAT="" CLAIM_OID_FILE=""
  FEATURE_CLAIM_ISSUE="" FEATURE_CLAIM_OID="" FEATURE_CLAIM_HEARTBEAT="" FEATURE_CLAIM_OID_FILE=""
  trap 'release_story_claim' EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  if ! CLAIM_OID="$("$RALPH_BIN" --claim "$issue" "$RALPH_CONFIG")"; then
    log "could not claim #$issue; skipping reconciliation for this tick"
    return 0
  fi
  CLAIM_ISSUE="$issue"
  start_claim_heartbeat "$issue"
  # A blocked Feature Story's arbitration is a unit of work on it like any
  # other; a PRD's own integration claims nothing more.
  claim_story_feature "$issue" || return 0
  "$@"
)

work_backlog() (
  # A subshell scopes the EXIT trap to story work: every early return releases
  # its Claim, before the reconciliation passes run in the parent Tick.
  CLAIM_ISSUE="" CLAIM_OID="" CLAIM_HEARTBEAT="" CLAIM_OID_FILE=""
  RALPH_CLAIM_ISSUE=""
  FEATURE_CLAIM_ISSUE="" FEATURE_CLAIM_OID="" FEATURE_CLAIM_HEARTBEAT="" FEATURE_CLAIM_OID_FILE=""
  trap 'release_story_claim' EXIT
  trap 'exit 143' TERM
  trap 'exit 130' INT
  local base_branch="$1"
  # --- work eligible stories in sequence (resume-first via the engine) ---
  # promo_failed bounds promotion retries: a green story whose promotion fails
  # stays in-progress, so resume-first would re-select it forever within this
  # tick. One failed promotion per story per tick; hitting it again ends the
  # tick cleanly (the next tick retries once, which heals transient gh errors).
  local n=0 action_line kind issue claimed skipped=""
  local -A promo_failed=()
  while (( n < RALPH_MAX_ITERATIONS )); do
    release_story_claim || return 2
    claimed="$("$RALPH_BIN" --claimed-issues)" || return 2
    action_line="$("$RALPH_BIN" --dry-run --claimed-issues "$claimed $skipped")" || return 2
    kind="${action_line%% *}"
    case "$kind" in
      no-work)
        log "no eligible work; tick complete"
        break
        ;;
      halt)
        log "loop halted (needs-human); tick complete"
        return 0
        ;;
      resume|start)
        issue="${action_line##*#}"
        if [[ -n "${promo_failed[$issue]:-}" ]]; then
          log "promotion of #$issue already failed this tick; ending tick instead of retry-looping (next tick retries)"
          return 0
        fi
        # Selection is a snapshot: another Slot can win before this push.
        # Losing the race spends no Attempt and selection tries the next Story.
        if ! CLAIM_OID="$("$RALPH_BIN" --claim "$issue" "$RALPH_CONFIG")"; then
          log "could not claim #$issue; skipping it for this tick"
          skipped="$skipped $issue"
          n=$(( n + 1 ))
          continue
        fi
        CLAIM_ISSUE="$issue"
        # What tells a fence that an *absent* Claim is a lost one rather than
        # an unclaimed mutation (#139): this Slot took one on this issue, so a
        # ref that is gone means the Slot that took it over has since released
        # it. Exported, so the agent's own calls read it too.
        export RALPH_CLAIM_ISSUE="$issue"
        start_claim_heartbeat "$issue"
        if ! claim_story_feature "$issue"; then
          skipped="$skipped $issue"
          n=$(( n + 1 ))
          continue
        fi
        # Before labels, assignment, review or checkout: an operator inspecting
        # this branch parks it for this Tick only, without spending an Attempt.
        local branch_resolved=no holding_worktree
        if resolve_story_branch "$issue"; then
          branch_resolved=yes
          if holding_worktree="$(story_branch_worktree "$STORY_BRANCH")"; then
            log "#$issue branch $STORY_BRANCH is held by worktree $holding_worktree; skipping it for this tick"
            skipped="$skipped $issue"
            n=$(( n + 1 ))
            continue
          fi
        fi
        log "$kind #$issue"
        # In Review has resume priority so the negotiation path owns the tick,
        # but it is never more implementation work: wait out the review window
        # here, holding the lock, and start no other Story while waiting. An
        # unrelated or unmarked pull request spends zero invocations -- the
        # tools refuse it before launching anything.
        if [[ "$kind" == "resume" ]] && \
           gh issue view "$issue" --json labels \
             | grep -q '"state:in-review"'; then
          local review_rc=0
          await_review "$issue" || review_rc=$?
          case "$review_rc" in
            0)
              log "review negotiation for #$issue needs no further waiting this tick"
              ;;
            "$RC_REVIEW_WINDOW")
              log "review window closed for #$issue; writing a Handoff and ending the tick"
              checkpoint_review "$issue"
              ;;
            "$RC_REVIEW_COMPLETED")
              # The Story is finished -- merged and closed, or parked at
              # awaiting-bench -- so it is off the backlog and the tick can
              # spend the rest of its budget on the next one.
              log "#$issue passed the review gate and was completed"
              n=$(( n + 1 ))
              continue
              ;;
            "$RC_CLAIM_LOST")
              # Another Slot holds this Story's Claim and its review with it.
              claim_lost "$issue"
              n=$(( n + 1 ))
              continue
              ;;
            "$RC_REVIEW_BLOCKED")
              # A model that kept answering with nothing usable spent the
              # Story's Attempts (#95): it is state:blocked, like a deadlock,
              # and the tick moves on -- the breaker decides for the loop.
              log "#$issue is blocked: its model produced nothing usable until its Attempts ran out"
              check_breaker
              n=$(( n + 1 ))
              continue
              ;;
            "$RC_REVIEW_ESCALATED")
              # Deadlock stops this Story, not the loop: it is already
              # state:blocked with a human asked to arbitrate, so it will not be
              # re-selected and the tick goes straight on to other work. The
              # breaker still gets its say on whether *this many* blocked
              # Stories mean the whole loop should stop.
              log "review for #$issue deadlocked; it is blocked and a human was asked to arbitrate"
              check_breaker
              n=$(( n + 1 ))
              continue
              ;;
            *)
              log "review step for #$issue did not complete (exit $review_rc); the next tick retries"
              # A refused response can itself have spent the Story's last
              # Attempt (#100), so the breaker gets its say here too.
              check_breaker
              ;;
          esac
          log "#$issue stays in review; the next tick resumes it first"
          return 0
        fi
        # `start` moves a state:ready story into state:in-progress up front, so a
        # checkpoint/partial pass/completion all see the expected state. `resume`
        # is already active (in-progress or in-review), so it is left alone.
        if [[ "$kind" == "start" ]]; then
          # Its own refusal, for the same reason the assignment below has one.
          if ! begin_story "$issue"; then
            n=$(( n + 1 ))
            continue
          fi
        fi
        # The unit's first write, so it is also where a Claim this Slot
        # already lost shows up first (#139): the Story belongs to another
        # Slot now, and this one must leave it alone.
        if ! assign_story_models "$issue"; then
          n=$(( n + 1 ))
          continue
        fi
        # Every unskipped Story consumes the same freshness backlog reads,
        # whether or not its branch resolved.
        local fresh
        fresh="$("$RALPH_BIN" --needs-freshness "$issue" 2>/dev/null || true)"
        if [[ "$branch_resolved" != yes ]]; then
          log "cannot resolve the branch for #$issue; leaving the checkout to the iteration"
        elif ! checkout_story_branch "$STORY_BRANCH" "$STORY_CUT_FROM" "$base_branch"; then
          log "could not check out $STORY_BRANCH for #$issue; ending tick (next tick retries)"
          return 1
        elif [[ "$fresh" == "yes" ]] && ! freshness_merge "$issue" "$base_branch"; then
          # Deterministic: the same merge conflicts again next pass, and only a
          # human can resolve it -- waiting on a human, not a failure (#114).
          if ! park_story "$issue" "Merge origin/$base_branch into \`$STORY_BRANCH\` by hand: the freshness merge conflicts in ${FRESHNESS_CONFLICTS:-(no paths reported)}. Push the merged branch, then remove needs-human."; then
            log "could not park #$issue; ending tick (next tick retries)"
            return 0
          fi
          n=$(( n + 1 ))
          continue
        fi
        # The Story branch's tree before the iteration: a Handoff that leaves it
        # unchanged is an idle one (#111). Empty when the branch is unresolved.
        local rc=0 handoffs_before tree_before=""
        if [[ -n "$STORY_BRANCH" ]]; then
          tree_before="$(git rev-parse -q --verify "refs/heads/$STORY_BRANCH^{tree}" 2>/dev/null || true)"
        fi
        handoffs_before="$(story_handoffs "$issue")"
        run_iteration "$kind" "$issue" || rc=$?
        case "$rc" in
          0)  # Stopped short of the done-signal. Partial progress only when
              # the iteration reached a normal boundary -- a Handoff (ADR-0004).
              # Otherwise it is a failed Attempt: treating every clean exit as
              # progress relaunched autopilot_controller #72 41 times over two
              # ticks with nothing recorded (#95).
            local handoffs_after
            handoffs_after="$(story_handoffs "$issue")"
            if [[ -n "$handoffs_before" && "$handoffs_after" == "$handoffs_before" ]]; then
              record_failed_attempt "$issue" "the iteration ended without the done-signal or a Handoff"
            elif [[ -n "$tree_before" && -n "$handoffs_after" ]] && \
                 [[ "$(git rev-parse -q --verify "refs/heads/$STORY_BRANCH^{tree}" 2>/dev/null || true)" == "$tree_before" ]]; then
              # A Handoff that changed nothing is not progress either: resumed
              # as-is it writes the same Handoff again, which is how
              # autopilot_controller #162 handed off 22 times in 40 minutes.
              # Counted, and at the limit the Story is parked on a human (#111).
              record_idle_handoff "$issue"
            fi
            ;;
          "$RC_INFRA_FAILURE")  # the provider ran and died; no progress to promote
            log "agent launch failed for #$issue (infrastructure); resuming it on a later pass"
            ;;
          "$RC_LAUNCH_UNAVAILABLE")
            # Ending the tick is not pessimism, it is the only useful move: no
            # agent ran, the story stays state:in-progress, and resume-first
            # selects it again on the very next pass -- so continuing re-runs the
            # identical refusal until the iteration bound. A non-zero exit also
            # lets the scheduler surface it: a tick that launched nothing must
            # not look like a clean one. The next tick retries from scratch.
            log "no agent could be launched for #$issue; ending tick (next tick retries)"
            return 1
            ;;
          "$RC_STORY_COMPLETE")  # green: promote it off the backlog
            # A done-signal that cannot be promoted -- no Story branch, nothing
            # committed -- is a failed Attempt (#98). Unrecorded, the Story
            # stayed in progress and was relaunched every tick, forever.
            # A promotion that was merely *fenced* is not a failed Attempt
            # (#139): the Story is another Slot's now, and charging it an
            # Attempt -- or retrying the promotion -- would be this Slot
            # writing to work it no longer owns.
            local promo_rc=0
            complete_story "$issue" || promo_rc=$?
            if (( promo_rc == RC_CLAIM_LOST )); then
              claim_lost "$issue"
            elif (( promo_rc != 0 )); then
              promo_failed[$issue]=1
              log "promotion of #$issue failed (see above); leaving it in-progress"
              record_failed_attempt "$issue" "the iteration signalled done but its work could not be promoted into review"
            fi
            ;;
          "$RC_NEEDS_HUMAN")
            # Unparked, resume-first would relaunch it at once to hear the
            # same thing again, so a failure here ends the tick instead.
            if ! park_story "$issue" "$NEEDS_HUMAN"; then
              log "could not park #$issue; ending tick (next tick retries)"
              return 0
            fi
            ;;
          "$RC_SESSION_LIMIT")
            checkpoint_story "$issue"
            return 0
            ;;
          *)
            log "run_iteration returned unexpected code $rc for #$issue; checkpointing"
            checkpoint_story "$issue"
            return 0
            ;;
        esac
        n=$(( n + 1 ))
        continue
        ;;
      *)
        log "unrecognized action from --dry-run: $action_line"
        return 2
        ;;
    esac
  done
  return 0
)

# Completion pass: scan for Features ready to integrate (ADR-0006, US-029).
# Each tick scans for eligible PRDs (all stories closed, PRD open + state:ready)
# and runs the completion pass for each. This fires even when the final story
# close was a human bench act between ticks.
completion_pass() {
  local prd_number
  while IFS= read -r prd_number; do
    [[ -z "$prd_number" ]] && continue
    reconcile_with_claim "$prd_number" complete_feature "$prd_number" \
      || log "completion pass for PRD #$prd_number failed (see above); continuing"
  done < <("$RALPH_BIN" --ready-features 2>/dev/null)
}

if [[ "${1:-}" == --preflight ]]; then
  preflight_only
else
  tick "$@"
fi
