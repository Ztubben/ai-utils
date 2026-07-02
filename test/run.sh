#!/usr/bin/env bash
# Ralph Loop test suite runner (low-verbosity green gate).
#
# Runs the fixture-driven Python unit tests for pure logic (config validator,
# selection engine) and, when `bats` is installed, the bats orchestration tests
# that drive bin/ralph against mocked `claude` and `gh` on PATH.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "== python unit tests =="
python3 -m unittest discover -s "$ROOT/test/unit" -p 'test_*.py' -q

if command -v bats >/dev/null 2>&1; then
  if compgen -G "$ROOT/test/bats/*.bats" >/dev/null; then
    echo "== bats orchestration tests =="
    bats "$ROOT"/test/bats/*.bats
  fi
else
  echo "== bats not installed; skipping orchestration tests =="
fi
