#!/usr/bin/env bats
# Orchestration tests for the Ralph tick loop (US-011, ADR-0002/0004).
#
# These drive bin/ralph.sh against mocked `claude`, `gh` and `git` on PATH. bats
# is auto-detected by test/run.sh; the same contract is also exercised by the
# stdlib-unittest gate test/unit/test_orchestrate.py (the executed gate where
# bats is not installed).

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  RALPH_SH="$REPO_ROOT/bin/ralph.sh"
  SP="$BATS_TEST_TMPDIR/sp"
  MB="$SP/mockbin"
  mkdir -p "$SP/.git" "$SP/ghq" "$MB"
  cp "$REPO_ROOT/test/fixtures/config/valid/full.yml" "$SP/.ralph.yml"

  export RALPH_LOG="$SP/ralph.log"
  export RALPH_GH_QUEUE_DIR="$SP/ghq"
  export RALPH_SESSION_LIMIT_EXIT=91
  export PATH="$MB:$PATH"

  cat >"$MB/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$RALPH_LOG"
if [[ "$1 $2" == "issue list" ]]; then
  n=$(cat "$RALPH_GH_QUEUE_DIR/counter" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$RALPH_GH_QUEUE_DIR/counter"
  f="$RALPH_GH_QUEUE_DIR/$n.json"
  if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
elif [[ "$1 $2" == "issue view" ]]; then
  cat "$RALPH_GH_QUEUE_DIR/story.json"
fi
EOF
  cat >"$MB/claude" <<'EOF'
#!/usr/bin/env bash
cat > /dev/null
echo "claude action=${RALPH_ITERATION_ACTION:-} issue=${RALPH_ITERATION_ISSUE:-}" >> "$RALPH_LOG"
[[ -n "${RALPH_CLAUDE_EMIT:-}" ]] && printf '%s\n' "$RALPH_CLAUDE_EMIT"
exit "${RALPH_CLAUDE_EXIT:-0}"
EOF
  cat >"$MB/git" <<'EOF'
#!/usr/bin/env bash
echo "git $*" >> "$RALPH_LOG"
exit 0
EOF
  chmod +x "$MB/gh" "$MB/claude" "$MB/git"
}

# A story issue in `gh --json` shape. $1=number $2=state $3=type(default afk).
story() {
  local n="$1" state="$2" type="${3:-afk}"
  printf '{"number":%d,"title":"Story %d","labels":[{"name":"type:%s"},{"name":"prio:1"},{"name":"state:%s"}],"body":"## Acceptance Criteria\\n- [ ] x\\n\\nDepends on: None\\n","state":"OPEN"}' \
    "$n" "$n" "$type" "$state"
}

@test "an overlapping tick exits immediately" {
  echo "[$(story 7 ready)]" > "$SP/ghq/0.json"
  exec 8>"$SP/.git/ralph-tick.lock"
  flock -n 8
  run bash -c "cd '$SP' && '$RALPH_SH'"
  flock -u 8
  [ "$status" -eq 0 ]
  [[ "$output" == *"already running"* ]]
  ! grep -q claude "$RALPH_LOG" 2>/dev/null
}

@test "resume-first: an in-progress story is resumed before ready work" {
  echo "[$(story 5 in-progress),$(story 7 ready)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [ "$(grep -c '^claude ' "$RALPH_LOG")" -eq 1 ]
  grep -q 'action=resume issue=5' "$RALPH_LOG"
}

@test "works multiple eligible stories in sequence" {
  echo "[$(story 7 ready),$(story 8 ready)]" > "$SP/ghq/0.json"
  echo "[$(story 8 ready)]" > "$SP/ghq/1.json"
  echo "[]" > "$SP/ghq/2.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [ "$(grep -c '^claude ' "$RALPH_LOG")" -eq 2 ]
}

@test "session-limit exhaustion checkpoints via Handoff and ends cleanly" {
  echo "[$(story 5 in-progress)]" > "$SP/ghq/0.json"
  story 5 in-progress > "$SP/ghq/story.json"
  RALPH_CLAUDE_EXIT=91 run bash -c "cd '$SP' && RALPH_CLAUDE_EXIT=91 '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [ "$(grep -c '^claude ' "$RALPH_LOG")" -eq 1 ]
  grep -q 'issue comment 5' "$RALPH_LOG"
  [[ "$output" == *"session limit"* ]]
}

@test "halt on needs-human without launching an iteration" {
  printf '[{"number":9,"title":"S","labels":[{"name":"type:afk"},{"name":"prio:1"},{"name":"state:ready"},{"name":"needs-human"}],"body":"## Acceptance Criteria\\n- [ ] x\\n\\nDepends on: None\\n","state":"OPEN"}]' > "$SP/ghq/0.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [[ "$output" == *"halt"* ]]
  ! grep -q claude "$RALPH_LOG" 2>/dev/null
}

@test "start moves a ready story to state:in-progress before iterating" {
  echo "[$(story 7 ready afk)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  grep -q 'gh issue edit 7 --add-label state:in-progress --remove-label state:ready' "$RALPH_LOG"
}

@test "resume does not re-label an already in-progress story" {
  echo "[$(story 5 in-progress afk)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  ! grep -q 'gh issue edit' "$RALPH_LOG"
}

@test "a green AFK story is auto-merged and closed (not re-selected)" {
  echo "[$(story 7 ready afk)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  story 7 ready afk > "$SP/ghq/story.json"
  run bash -c "cd '$SP' && RALPH_CLAUDE_EMIT=RALPH-STORY-COMPLETE '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [ "$(grep -c '^claude ' "$RALPH_LOG")" -eq 1 ]
  grep -q 'gh pr merge' "$RALPH_LOG"
  grep -q 'gh issue close 7' "$RALPH_LOG"
}

@test "a green HIL story opens a PR and moves to awaiting-bench" {
  echo "[$(story 5 in-progress hil)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  story 5 in-progress hil > "$SP/ghq/story.json"
  run bash -c "cd '$SP' && RALPH_CLAUDE_EMIT=RALPH-STORY-COMPLETE '$RALPH_SH'"
  [ "$status" -eq 0 ]
  grep -q 'gh pr create' "$RALPH_LOG"
  grep -q 'state:awaiting-bench' "$RALPH_LOG"
  ! grep -q 'gh pr merge' "$RALPH_LOG"
  ! grep -q 'gh issue close' "$RALPH_LOG"
}

@test "a partial iteration (no done-signal) is not promoted" {
  echo "[$(story 7 ready afk)]" > "$SP/ghq/0.json"
  echo "[]" > "$SP/ghq/1.json"
  story 7 ready afk > "$SP/ghq/story.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  grep -q '^claude ' "$RALPH_LOG"
  ! grep -q 'gh pr merge' "$RALPH_LOG"
  ! grep -q 'gh pr create' "$RALPH_LOG"
}
