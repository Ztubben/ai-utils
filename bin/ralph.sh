#!/usr/bin/env bash
# ralph.sh - the Ralph Loop unattended tick (US-011, ADR-0002/0004).
#
# One tick is a single scheduled run (the scheduler fires it every 3 hours,
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
#   5. when `claude` signals session-limit exhaustion, checkpoints the current
#      story via a Handoff (`ralph --checkpoint`) and ends the tick cleanly.
#
# Ralph only ever modifies the superproject and never touches main. The heavy
# lifting (TDD, gating, completion) lives in the `claude` iteration driven by
# prompts/iterate.v1.md; this script is only the orchestration shell, kept thin
# so it can be driven by tests against mocked `claude`/`gh` on PATH.
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

log() { printf 'ralph: %s\n' "$*"; }

# Launch one fresh-context claude iteration for the selected story. Returns 0
# normally, or RC_SESSION_LIMIT (10) when claude signalled session-limit
# exhaustion (by exit code or output marker).
RC_SESSION_LIMIT=10
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
        if run_iteration "$kind" "$issue"; then
          n=$(( n + 1 ))
          continue
        else
          checkpoint_story "$issue"
          return 0
        fi
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
