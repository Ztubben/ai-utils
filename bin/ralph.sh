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
#      promotes it: `ralph --complete-afk` (auto-merge → base) for a type:afk
#      story, `ralph --complete-hil` (open PR → state:awaiting-bench) for HIL.
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
# type:afk -> --complete-afk (auto-merge into base, close), type:hil ->
# --complete-hil (open PR, move to state:awaiting-bench). The completion tools
# refuse to touch main and re-validate the type, so this stays a thin dispatch.
# The story is fetched fresh so completion has the full record. Returns non-zero
# on a git/gh/dispatch failure; the caller logs and moves on.
complete_story() {
  local issue="$1" story_json
  story_json="$(gh issue view "$issue" --json number,title,labels,body,state)"
  if grep -q '"type:afk"' <<<"$story_json"; then
    log "green #$issue is type:afk; auto-merging (--complete-afk)"
    "$RALPH_BIN" --complete-afk - "$RALPH_CONFIG" <<<"$story_json"
  elif grep -q '"type:hil"' <<<"$story_json"; then
    log "green #$issue is type:hil; opening PR (--complete-hil)"
    "$RALPH_BIN" --complete-hil - "$RALPH_CONFIG" <<<"$story_json"
  else
    log "cannot promote #$issue: no type:afk/type:hil label"
    return 2
  fi
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

  # --- work eligible stories in sequence (resume-first via the engine) ---
  local n=0 action_line kind issue
  while (( n < RALPH_MAX_ITERATIONS )); do
    action_line="$("$RALPH_BIN" --dry-run)"
    kind="${action_line%% *}"
    case "$kind" in
      no-work)
        log "no eligible work; tick complete"
        return 0
        ;;
      halt)
        log "loop halted (needs-human); tick complete"
        return 0
        ;;
      resume|start)
        issue="${action_line##*#}"
        log "$kind #$issue"
        # `start` moves a state:ready story into state:in-progress up front, so a
        # checkpoint/partial pass/completion all see the expected state. `resume`
        # is already in-progress (a prior tick moved it), so it is left alone.
        if [[ "$kind" == "start" ]]; then
          begin_story "$issue"
        fi
        local rc=0
        run_iteration "$kind" "$issue" || rc=$?
        case "$rc" in
          0)  # partial progress: resume the same story on the next pass
            ;;
          "$RC_STORY_COMPLETE")  # green: promote it off the backlog
            complete_story "$issue" \
              || log "promotion of #$issue failed (see above); leaving it in-progress"
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

  log "reached max iterations ($RALPH_MAX_ITERATIONS) this tick; stopping"
  return 0
}

tick "$@"
