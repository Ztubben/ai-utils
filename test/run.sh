#!/usr/bin/env bash
# Ralph Loop test suite runner (low-verbosity green gate).
#
# Runs every test tier that exists, and refuses to report green unless all of
# them ran (#86): a gate that quietly skips a tier makes "green" mean "part of
# the suite ran", locally and in the Loop's own gating on ai-utils alike.
#
#   unit       Python `unittest` over test/unit (pure logic, fixture-driven).
#   bats       bats orchestration tests driving bin/ralph against mocked
#              provider CLIs and `gh` on PATH. bats is REQUIRED.
#   scenarios  multi-Tick scenario harness (test/scenarios, PRD #85), once present.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prerequisites are checked before any tier runs, so a missing tool fails in a
# second rather than after the whole unit tier.
if ! command -v bats >/dev/null 2>&1; then
  echo "test/run.sh: bats is not installed; the orchestration tier cannot run." >&2
  echo "Install it (e.g. 'apt-get install bats' or 'brew install bats-core') — the gate never skips a tier." >&2
  exit 1
fi

ran=()

echo "== unit tests =="
python3 -m unittest discover -s "$ROOT/test/unit" -p 'test_*.py' -q
ran+=(unit)

if compgen -G "$ROOT/test/bats/*.bats" >/dev/null; then
  echo "== bats orchestration tests =="
  bats "$ROOT"/test/bats/*.bats
  ran+=(bats)
fi

if [ -d "$ROOT/test/scenarios" ]; then
  echo "== scenario tests =="
  python3 -m unittest discover -s "$ROOT/test/scenarios" -t "$ROOT/test/scenarios" -p 'test_*.py' -q
  ran+=(scenarios)
fi

echo "== tiers run: ${ran[*]} =="
