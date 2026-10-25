#!/usr/bin/env bats
# The tick puts the checkout on the selected Story's own branch before it syncs
# or freshness-merges anything (#121).
#
# Unlike orchestration.bats, git here is real: the defect was which branch a
# merge landed on and what the working tree looked like afterwards, which a
# logging git mock cannot show. gh and the provider CLIs are still mocks.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  RALPH_SH="$REPO_ROOT/bin/ralph.sh"
  RALPH="$REPO_ROOT/bin/ralph"
  if [[ -z "${BATS_TEST_TMPDIR:-}" ]]; then
    BATS_TEST_TMPDIR="$(mktemp -d)"
    RALPH_OWN_TMPDIR="$BATS_TEST_TMPDIR"
  fi
  SP="$BATS_TEST_TMPDIR/sp"
  # Where the tick actually works: its Slot, a worktree of $SP (#134). The
  # operator's checkout is left alone, so every assertion about a branch being
  # checked out, a merge in flight or a dirty tree belongs here.
  SLOT="$SP.ralph-slot-1"
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  MB="$BATS_TEST_TMPDIR/mockbin"
  GHQ="$BATS_TEST_TMPDIR/ghq"
  mkdir -p "$SP" "$GHQ" "$MB"

  export RALPH_LOG="$BATS_TEST_TMPDIR/ralph.log"
  export RALPH_GH_QUEUE_DIR="$GHQ"
  export RALPH_SESSION_LIMIT_EXIT=91
  unset RALPH_ITERATION_ACTION RALPH_ITERATION_ISSUE RALPH_SESSION_LIMIT_MARKER \
        RALPH_CONFIG RALPH_MAX_ITERATIONS RALPH_CLI \
        RALPH_TOOLING_COMMIT RALPH_TOOLING_KIND RALPH_TOOLING_MOUNT RALPH_CLAUDE RALPH_CODEX \
        RALPH_SLOT RALPH_SLOT_DIR
  # The world's git must not read the developer's own configuration.
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export PATH="$MB:$PATH"

  # `issue list` pops one backlog per call; `issue view N` answers view-N.json.
  cat >"$MB/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$RALPH_LOG"
if [[ "$1 $2" == "issue list" ]]; then
  n=$(cat "$RALPH_GH_QUEUE_DIR/counter" 2>/dev/null || echo 0)
  echo $((n + 1)) > "$RALPH_GH_QUEUE_DIR/counter"
  f="$RALPH_GH_QUEUE_DIR/$n.json"
  if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
elif [[ "$1 $2" == "issue view" ]]; then
  cat "$RALPH_GH_QUEUE_DIR/view-$3.json"
fi
EOF
  # The agent records the branch it was launched on, and does nothing else.
  for provider in claude codex; do
    cat >"$MB/$provider" <<EOF
#!/usr/bin/env bash
cat > /dev/null
echo "$provider issue=\${RALPH_ITERATION_ISSUE:-} branch=\$(git rev-parse --abbrev-ref HEAD)" >> "\$RALPH_LOG"
EOF
    chmod +x "$MB/$provider"
  done
  chmod +x "$MB/gh"

  git init -q --bare -b main "$ORIGIN"
  git -C "$SP" init -q -b main
  git -C "$SP" remote add origin "$ORIGIN"
  sed 's/base: develop/base: main/' "$REPO_ROOT/test/fixtures/config/valid/full.yml" >"$SP/.ralph.yml"
  echo a >"$SP/shared.txt"
  commit "$SP" init
  git -C "$SP" push -q origin main
}

teardown() {
  if [[ -n "${RALPH_OWN_TMPDIR:-}" ]]; then
    rm -rf "$RALPH_OWN_TMPDIR"
  fi
}

commit() {
  git -C "$1" add -A
  git -C "$1" commit -q -m "$2"
}

# An issue in `gh --json` shape. $1=number $2=title $3=labels(json names) $4=Parent $5=Depends $6=state
issue() {
  local labels="" name
  for name in $3; do labels+="${labels:+,}{\"name\":\"$name\"}"; done
  printf '{"number":%d,"title":"%s","labels":[%s],"body":"## Acceptance Criteria\\n- [ ] x\\n\\nParent: %s\\nDepends on: %s\\n","state":"%s"}' \
    "$1" "$2" "$labels" "$4" "$5" "${6:-OPEN}"
}

# The world of autopilot_controller #183: the previous tick left the checkout on
# a local-only Story branch of Feature #52, which forked before main moved on.
# Story #183 belongs to Feature #181. $1 = "fresh" when Feature #181's branch
# already contains main, "stale" when it forked before main moved and conflicts.
cross_feature_world() {
  local feature181="feature/181-prd-cad"
  issue 181 "PRD: cad" "prd state:ready" None None >"$GHQ/view-181.json"
  issue 4 "Done orphan" "type:afk" None None CLOSED >"$GHQ/view-4.json"
  issue 183 "Story 183" "type:afk prio:1 state:ready" "#181" "#4" >"$GHQ/view-183.json"
  STORY_BRANCH="$("$RALPH" --branch-name "$GHQ/view-183.json" "$SP/.ralph.yml" "$GHQ/view-181.json")"
  [ "$("$RALPH" --branch-name "$GHQ/view-183.json" "$SP/.ralph.yml" "$GHQ/view-181.json" --base)" = "$feature181" ]

  git -C "$SP" switch -q -c feature/52-a
  echo feature-a >"$SP/shared.txt"; commit "$SP" a
  git -C "$SP" push -q origin feature/52-a
  if [[ "$1" == stale ]]; then
    git -C "$SP" switch -q -c "$feature181" main
    echo feature-181 >"$SP/shared.txt"; commit "$SP" f181
    git -C "$SP" push -q origin "$feature181"
  fi
  git -C "$SP" switch -q main
  echo main-moved >"$SP/shared.txt"; commit "$SP" moved
  git -C "$SP" push -q origin main
  if [[ "$1" == fresh ]]; then
    git -C "$SP" branch -q "$feature181" main
    git -C "$SP" push -q origin "$feature181"
  fi
  # Story #65's branch: local only (its remote was deleted on merge).
  git -C "$SP" switch -q -c ralph/65-story feature/52-a
  echo story65 >"$SP/shared.txt"; commit "$SP" story65
  PREVIOUS_HEAD="$(git -C "$SP" rev-parse HEAD)"

  local backlog
  backlog="[$(cat "$GHQ/view-181.json"),$(cat "$GHQ/view-4.json"),$(cat "$GHQ/view-183.json")]"
  echo "$backlog" >"$GHQ/0.json"   # --dry-run: start #183
  echo "$backlog" >"$GHQ/1.json"   # --needs-freshness 183: yes
}

@test "a Story is started on its own branch, not on the previous Story's (#121)" {
  cross_feature_world fresh
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  grep -q "^claude issue=183 branch=$STORY_BRANCH\$" "$RALPH_LOG"
  # The previous Story's branch was neither reset nor merged into.
  [ "$(git -C "$SP" rev-parse ralph/65-story)" = "$PREVIOUS_HEAD" ]
  [ -z "$(git -C "$SLOT" rev-parse -q --verify MERGE_HEAD)" ]
  # Its Feature branch already contains main, so there was nothing to merge.
  [[ "$output" == *"already contains origin/main"* ]]
  [ "$(git -C "$SP" rev-list --merges "$STORY_BRANCH" | wc -l)" -eq 0 ]
}

@test "a conflicting freshness merge is aborted and the Story parked on a human (#121, #114)" {
  cross_feature_world stale
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ -z "$(git -C "$SLOT" rev-parse -q --verify MERGE_HEAD)" ]
  [ -z "$(git -C "$SLOT" ls-files -u)" ]
  [ -z "$(git -C "$SLOT" status --porcelain --untracked-files=no)" ]
  ! grep -q '^claude ' "$RALPH_LOG"
  [[ "$output" == *"shared.txt"* ]]
  # Only a human can merge it: the Story is parked, naming the paths -- not an
  # Attempt, so it is never blocked and never counts toward the breaker.
  grep -q '^gh issue comment 183 ' "$RALPH_LOG"
  grep -q 'conflicts in shared.txt' "$RALPH_LOG"
  grep -q '^gh issue edit 183 --add-label needs-human' "$RALPH_LOG"
  ! grep -q 'ralph:attempt' "$RALPH_LOG"
  [ "$(git -C "$SP" rev-parse ralph/65-story)" = "$PREVIOUS_HEAD" ]
}

@test "a tick runs through a checkout the operator left mid-merge (#134)" {
  cross_feature_world fresh
  git -C "$SP" merge -q origin/main >/dev/null 2>&1 || true
  [ -e "$SP/.git/MERGE_HEAD" ]
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  [[ "$output" != *"refusing to tick"* ]]
  grep -q "^claude issue=183 branch=$STORY_BRANCH\$" "$RALPH_LOG"
  # The operator's half-finished merge is still theirs to finish.
  [ -e "$SP/.git/MERGE_HEAD" ]
}

@test "a tick refuses to run on a slot left mid-merge (#121, #134)" {
  cross_feature_world fresh
  # A first tick provisions the Slot; a crash then leaves it mid-merge.
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 0 ]
  git -C "$SLOT" merge -q origin/main >/dev/null 2>&1 || true
  git -C "$SLOT" rev-parse -q --verify MERGE_HEAD >/dev/null \
    || echo "$(git -C "$SLOT" rev-parse HEAD)" >"$(git -C "$SLOT" rev-parse --absolute-git-dir)/MERGE_HEAD"
  : >"$RALPH_LOG"
  echo "[$(cat "$GHQ/view-181.json"),$(cat "$GHQ/view-4.json"),$(cat "$GHQ/view-183.json")]" >"$GHQ/2.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  [ "$status" -eq 2 ]
  [[ "$output" == *"mid-merge"* ]]
  [[ "$output" == *"$SLOT"* ]]
  ! grep -q 'gh issue edit' "$RALPH_LOG"
  ! grep -q '^claude ' "$RALPH_LOG"
}

@test "a resumed Story keeps its unpushed local commits (#121)" {
  issue 7 "Story 7" "type:afk prio:1 state:in-progress" None None >"$GHQ/view-7.json"
  STORY_BRANCH="$("$RALPH" --branch-name "$GHQ/view-7.json" "$SP/.ralph.yml")"
  git -C "$SP" switch -q -c "$STORY_BRANCH"
  echo pushed >"$SP/s7.txt"; commit "$SP" pushed
  git -C "$SP" push -q origin "$STORY_BRANCH"
  echo local >>"$SP/s7.txt"; commit "$SP" local-only
  local ahead; ahead="$(git -C "$SP" rev-parse HEAD)"
  git -C "$SP" switch -q main
  echo "[$(cat "$GHQ/view-7.json")]" >"$GHQ/0.json"
  echo "[$(cat "$GHQ/view-7.json")]" >"$GHQ/1.json"
  run bash -c "cd '$SP' && '$RALPH_SH'"
  grep -q "^claude issue=7 branch=$STORY_BRANCH\$" "$RALPH_LOG"
  [ "$(git -C "$SP" rev-parse "$STORY_BRANCH")" = "$ahead" ]
}
