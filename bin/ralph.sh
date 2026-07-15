#!/usr/bin/env bash
# ralph.sh - the Ralph Loop unattended tick (US-011, ADR-0002/0004).
#
# One tick is a single scheduled run (the scheduler fires it every 5 hours,
# US-012). A tick:
#   1. guards with flock so only one tick per superproject runs at a time; an
#      overlapping tick exits immediately (lockfile in .git/, ADR: Tick).
#   2. validates .ralph.yml at tick start (fails loud, ADR-0001).
#   3. drives the pure selection engine (`ralph --dry-run`) which is resume-first:
#      any state:in-progress story is resumed before scanning for new state:ready
#      work.
#   4. launches a fresh-context `claude` iteration per selected story and works
#      as many eligible stories in sequence as the session budget allows, until
#      no eligible work remains (no-work) or the loop halts (needs-human).
#   5. when an iteration signals the story is green (the done-signal marker),
#      promotes it: `ralph --complete-afk` for a type:afk story,
#      `ralph --complete-hil` (park at state:awaiting-bench) for HIL; both
#      branch on Feature membership per ADR-0006.
#      Without this promotion the still-in-progress story would be re-selected
#      forever (the engine is resume-first), so a green story must move off the
#      backlog before the loop advances.
#   6. when `claude` signals session-limit exhaustion, checkpoints the current
#      story via a Handoff (`ralph --checkpoint`) and ends the tick cleanly.
#
# Ralph only ever modifies the superproject and never touches main. The heavy
# lifting (TDD, gating) lives in the `claude` iteration driven by
# prompts/iterate.v1.md, which reports a green story via a done-signal marker;
# this script is only the orchestration shell -- selecting, promoting green
# stories, and checkpointing -- kept thin so it can be driven by tests against
# mocked `claude`/`gh`/`git` on PATH.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RALPH_BIN="${RALPH_CLI:-$SCRIPT_DIR/ralph}"
ITERATE_PROMPT="$SCRIPT_DIR/../prompts/iterate.v1.md"

# Tunables (env-overridable so tests and superprojects can adjust them).
: "${RALPH_LOCK_DIR:=.git}"                 # flock lives in the superproject's .git/
: "${RALPH_CONFIG:=.ralph.yml}"
: "${RALPH_CLAUDE:=claude}"
: "${RALPH_MAX_ITERATIONS:=25}"             # safety bound on stories per tick
: "${RALPH_SESSION_LIMIT_EXIT:=91}"         # claude CLI exit signalling session-limit exhaustion
: "${RALPH_SESSION_LIMIT_MARKER:=usage limit reached}"
: "${RALPH_STORY_COMPLETE_MARKER:=RALPH-STORY-COMPLETE}"  # iteration's green/done-signal (prompts/iterate.v1.md)

log() { printf 'ralph: %s\n' "$*"; }

# Read the configured base branch from .ralph.yml (default 'develop').
# Called once per tick after config validation; cached in BASE_BRANCH.
read_base_branch() {
  python3 -c "
import sys; sys.path.insert(0, '$(dirname "$RALPH_BIN")/../lib')
import ralph_config
r = ralph_config.load_and_validate('$RALPH_CONFIG')
print(r.config['branching']['base'] if r.ok else 'develop')
"
}

# Freshness merge (ADR-0006): when a Feature story has out-of-Feature deps
# (closed Orphan Stories or PRDs whose code landed in the base branch after
# the feature branch forked), merge the base branch into the feature branch
# before the iteration. A merge, never a rebase, so bench anchors survive.
freshness_merge() {
  local issue="$1" base="$2" answer
  answer="$("$RALPH_BIN" --needs-freshness "$issue")" || return 0
  if [[ "$answer" == "yes" ]]; then
    log "freshness merge: merging $base into feature branch for #$issue"
    git merge "$base" --no-edit \
      || { log "freshness merge failed for #$issue; continuing"; return 0; }
  fi
}

# Hard-sync the working branch from origin before an iteration starts, so that
# human history rewrites (allowed on feature branches) never collide with a
# stale local checkout (ADR-0006, US-029).
sync_branch() {
  local branch
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 0
  git fetch origin || return 0
  git reset --hard "origin/$branch" 2>/dev/null || true
}

# Launch one fresh-context claude iteration for the selected story. Returns:
#   RC_SESSION_LIMIT (10) when claude signalled session-limit exhaustion (by exit
#                         code or output marker) -- the story gets checkpointed;
#   RC_STORY_COMPLETE (11) when the iteration emitted the done-signal marker,
#                         meaning the gate is green and every acceptance criterion
#                         is checked -- the story gets promoted;
#   0                     otherwise (partial progress -- resume it next pass).
# Session-limit takes priority: a truncated run never counts as complete.
RC_SESSION_LIMIT=10
RC_STORY_COMPLETE=11
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

  set +e
  out="$(printf '%s' "$prompt" | "$RALPH_CLAUDE" --dangerously-skip-permissions --print 2>&1)"
  rc=$?
  set -e
  [[ -n "$out" ]] && printf '%s\n' "$out"

  if [[ "$rc" -eq "$RALPH_SESSION_LIMIT_EXIT" ]] \
     || printf '%s' "$out" | grep -qiF "$RALPH_SESSION_LIMIT_MARKER"; then
    return "$RC_SESSION_LIMIT"
  fi
  if printf '%s' "$out" | grep -qF "$RALPH_STORY_COMPLETE_MARKER"; then
    return "$RC_STORY_COMPLETE"
  fi
  return 0
}

# Write a Handoff for the current in-progress story and end the tick cleanly.
# The story is fetched fresh from gh so `ralph --checkpoint` has the full record.
checkpoint_story() {
  local issue="$1"
  log "session limit reached; checkpointing #$issue via Handoff"
  gh issue view "$issue" \
     --json number,title,labels,body,comments,state \
   | "$RALPH_BIN" --checkpoint - "Session limit reached; resume next tick." "$RALPH_CONFIG"
}

# Move a freshly-selected story from state:ready to state:in-progress before its
# first iteration. This is the `start` edge of the state machine that the rest of
# the loop assumes exists: a mid-iteration checkpoint then resumes it (resume-first
# needs state:in-progress), a partial pass is re-selected as `resume` not `start`,
# and HIL/AFK completion's `--remove-label state:in-progress` has a label to move.
# Best-effort: a failure here almost always means the labels were never created,
# so point at `ralph --init` and continue (the iteration can still do work).
begin_story() {
  local issue="$1"
  log "moving #$issue to state:in-progress"
  gh issue edit "$issue" \
     --add-label state:in-progress --remove-label state:ready \
   || log "could not label #$issue state:in-progress (are the Ralph labels created? run 'ralph --init'); continuing"
}

# Promote a green story off the backlog. Reads the story's type:* label and
# dispatches to the completion CLI that owns the label move / PR / merge:
# type:afk -> --complete-afk (close as Passing), type:hil -> --complete-hil
# (park at state:awaiting-bench); each branches on Feature membership per
# ADR-0006 (Feature stories push to the feature branch, no PR). The tools
# refuse to touch main and re-validate the type, so this stays a thin dispatch.
# The story is fetched fresh so completion has the full record. A Feature story
# (Parent: #N, ADR-0006) needs its PRD issue to resolve the feature branch, so
# the PRD is fetched and handed to the completion CLI as a temp file; an Orphan
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
  if grep -q '"type:afk"' <<<"$story_json"; then
    log "green #$issue is type:afk; completing (--complete-afk)"
    "$RALPH_BIN" --complete-afk - "$RALPH_CONFIG" ${prd_file:+"$prd_file"} \
      <<<"$story_json" || rc=$?
    # The close leaves the stale state label on the closed issue; strip it.
    if (( rc == 0 )); then
      gh issue edit "$issue" --remove-label state:in-progress >/dev/null 2>&1 || true
    fi
  elif grep -q '"type:hil"' <<<"$story_json"; then
    log "green #$issue is type:hil; completing (--complete-hil)"
    "$RALPH_BIN" --complete-hil - "$RALPH_CONFIG" ${prd_file:+"$prd_file"} \
      <<<"$story_json" || rc=$?
  else
    log "cannot promote #$issue: no type:afk/type:hil label"
    rc=2
  fi
  if [[ -n "$prd_file" ]]; then rm -f "$prd_file"; fi
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
  log "eligible PRD #$prd_number; running completion pass (--complete-feature)"
  "$RALPH_BIN" --complete-feature "$prd_file" "$RALPH_CONFIG" || rc=$?
  rm -f "$prd_file"
  return "$rc"
}

tick() {
  # --- flock: one tick per superproject, overlapping ticks exit immediately ---
  local lock_file="$RALPH_LOCK_DIR/ralph-tick.lock"
  exec 9>"$lock_file"
  if ! flock -n 9; then
    log "a tick is already running (lock held); exiting"
    return 0
  fi

  # --- config validation at tick start (ADR-0001: fail loud) ---
  if ! "$RALPH_BIN" --check-config "$RALPH_CONFIG" >/dev/null; then
    log "invalid or missing $RALPH_CONFIG; refusing to tick"
    return 2
  fi

  # --- read the base branch once (for freshness merges) ---
  local base_branch
  base_branch="$(read_base_branch)"

  # --- work eligible stories in sequence (resume-first via the engine) ---
  # promo_failed bounds promotion retries: a green story whose promotion fails
  # stays in-progress, so resume-first would re-select it forever within this
  # tick. One failed promotion per story per tick; hitting it again ends the
  # tick cleanly (the next tick retries once, which heals transient gh errors).
  local n=0 action_line kind issue
  local -A promo_failed=()
  while (( n < RALPH_MAX_ITERATIONS )); do
    action_line="$("$RALPH_BIN" --dry-run)"
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
        log "$kind #$issue"
        # `start` moves a state:ready story into state:in-progress up front, so a
        # checkpoint/partial pass/completion all see the expected state. `resume`
        # is already in-progress (a prior tick moved it), so it is left alone.
        if [[ "$kind" == "start" ]]; then
          begin_story "$issue"
        fi
        sync_branch
        freshness_merge "$issue" "$base_branch"
        local rc=0
        run_iteration "$kind" "$issue" || rc=$?
        case "$rc" in
          0)  # partial progress: resume the same story on the next pass
            ;;
          "$RC_STORY_COMPLETE")  # green: promote it off the backlog
            complete_story "$issue" \
              || { promo_failed[$issue]=1
                   log "promotion of #$issue failed (see above); leaving it in-progress"; }
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

  # --- completion pass: scan for Features ready to integrate (ADR-0006, US-029) ---
  # Each tick scans for eligible PRDs (all stories closed, PRD open + state:ready)
  # and runs the completion pass for each. This fires even when the final story
  # close was a human bench act between ticks.
  local prd_number
  while IFS= read -r prd_number; do
    [[ -z "$prd_number" ]] && continue
    complete_feature "$prd_number" \
      || log "completion pass for PRD #$prd_number failed (see above); continuing"
  done < <("$RALPH_BIN" --ready-features 2>/dev/null)

  return 0
}

tick "$@"
