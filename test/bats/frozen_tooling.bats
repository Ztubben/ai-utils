#!/usr/bin/env bats
# A self-hosted tick runs a frozen copy of origin/main, not the checkout (#146,
# ADR-0009).
#
# The world is ai-utils as its own target repository, on real git with a bare
# origin: the checkout's bin/ralph.sh and lib/ are changed the way a half-written
# Story leaves them, and the tick -- and the agent it launches -- must still run
# origin/main's code. gh and the provider CLIs are mocks. The Python harness
# (test/unit/test_frozen_tooling.py) covers the rest of the Story.

setup() {
  REPO_ROOT="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  if [[ -z "${BATS_TEST_TMPDIR:-}" ]]; then
    BATS_TEST_TMPDIR="$(mktemp -d)"
    RALPH_OWN_TMPDIR="$BATS_TEST_TMPDIR"
  fi
  SP="$BATS_TEST_TMPDIR/ai-utils"
  ORIGIN="$BATS_TEST_TMPDIR/origin.git"
  MB="$BATS_TEST_TMPDIR/mockbin"
  GHQ="$BATS_TEST_TMPDIR/ghq"
  mkdir -p "$SP" "$GHQ" "$MB"

  export RALPH_LOG="$BATS_TEST_TMPDIR/ralph.log"
  export RALPH_GH_QUEUE_DIR="$GHQ"
  export RALPH_MAX_ITERATIONS=1
  unset RALPH_ITERATION_ACTION RALPH_ITERATION_ISSUE RALPH_SESSION_LIMIT_MARKER \
        RALPH_CONFIG RALPH_CLI RALPH_CLAUDE RALPH_CODEX \
        RALPH_TOOLING_COMMIT RALPH_TOOLING_KIND RALPH_TOOLING_MOUNT
  export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
  export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
  export PATH="$MB:$PATH"

  cat >"$MB/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$RALPH_LOG"
if [[ "$1 $2" == "issue list" ]]; then
  cat "$RALPH_GH_QUEUE_DIR/backlog.json"
elif [[ "$1 $2" == "issue view" ]]; then
  cat "$RALPH_GH_QUEUE_DIR/story.json"
fi
EOF
  # The agent records which `ralph` its PATH resolves, and does nothing else.
  for provider in claude codex; do
    cat >"$MB/$provider" <<EOF
#!/usr/bin/env bash
cat > /dev/null
echo "$provider ralph=\$(command -v ralph)" >> "\$RALPH_LOG"
EOF
    chmod +x "$MB/$provider"
  done
  chmod +x "$MB/gh"
  story='{"number":7,"title":"Story 7","labels":[{"name":"type:afk"},{"name":"prio:1"},{"name":"state:ready"}],"body":"## Acceptance Criteria\n- [ ] x\n\nParent: None\nDepends on: None\n","state":"OPEN"}'
  echo "[$story]" >"$GHQ/backlog.json"
  echo "$story" >"$GHQ/story.json"

  git init -q --bare -b main "$ORIGIN"
  git -C "$SP" init -q -b main
  git -C "$SP" remote add origin "$ORIGIN"
  for part in bin lib schema prompts; do cp -r "$REPO_ROOT/$part" "$SP/$part"; done
  find "$SP" -name __pycache__ -prune -exec rm -rf {} +
  cp "$REPO_ROOT/test/fixtures/config/valid/full.yml" "$SP/.ralph.yml"
  echo "__pycache__/" >"$SP/.gitignore"
  git -C "$SP" add -A
  git -C "$SP" commit -q -m "ai-utils"
  git -C "$SP" push -q origin main
  MAIN="$(git -C "$SP" rev-parse HEAD)"
  COPY="$SP/.git/ralph-tooling/$MAIN"
}

teardown() {
  if [[ -n "${RALPH_OWN_TMPDIR:-}" ]]; then
    rm -rf "$RALPH_OWN_TMPDIR"
  fi
}

@test "a half-written checkout does not run: the tick and its agent run origin/main's frozen copy" {
  sed -i 's/log "no eligible work; tick complete"/log "checkout code ran"/' "$SP/bin/ralph.sh"
  printf '\nraise SystemExit("checkout code ran")\n' >>"$SP/lib/ralph_select.py"

  run bash -c "cd '$SP' && '$SP/bin/ralph.sh'"

  [ "$status" -eq 0 ]
  [[ "$output" != *"checkout code ran"* ]]
  [[ "$output" == *"running the self-hosted Tooling Commit $MAIN from $COPY"* ]]
  [ "$(git -C "$COPY" rev-parse HEAD)" = "$MAIN" ]
  grep -qxF "claude ralph=$COPY/bin/ralph" "$RALPH_LOG"
  ! grep -E '^(claude|codex) ' "$RALPH_LOG" | grep -vqF "ralph=$COPY/bin/ralph"
}
