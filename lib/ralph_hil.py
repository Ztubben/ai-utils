"""HIL completion for the Ralph Loop (US-007, ADR-0001, ADR-0003).

For a green `type:hil` story Ralph opens a PR to base and moves the issue to
`state:awaiting-bench`, then STOPS -- it never merges a HIL story and never
closes the issue, so the human bench-tests one clean diff off base in isolation.
A HIL story parked at `state:awaiting-bench` is not Passing (the issue stays
open), so it does not satisfy dependents' `Depends on:` edges (ADR-0002); only a
human bench-verifying and closing it does. Ralph integrates into base and
**never touches main** (ADR-0001).

The deterministic, host-testable seam mirrors AFK completion: a pure command
*plan* (`hil_complete_plan`) returns the ordered git/gh commands (as argv lists)
without running anything, so the PR-not-merge policy and the main-safety guard
are unit-testable. `run_plan` executes a plan fail-fast; the CLI wrapper
(`ralph --complete-hil`) prints and sets exit codes.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_story  # noqa: E402

PROTECTED_BRANCH = "main"
AWAITING_BENCH_LABEL = "state:awaiting-bench"
IN_PROGRESS_LABEL = "state:in-progress"
DEFAULT_BASE = "develop"


class Plan:
    def __init__(self, ok, errors, commands, base=None, branch=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.branch = branch


def hil_complete_plan(story, base=DEFAULT_BASE,
                      branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN):
    """Build the ordered command plan to open a PR for a green HIL story and
    move it to state:awaiting-bench.

    Pure: computes commands, runs nothing. Refuses (ok=False, no commands) when
    base is `main` (never touches main) or the story is not `type:hil` (AFK
    auto-merge is US-006's job). Never emits a merge or a close: the human
    bench-verifies and merges the clean diff.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to open a PR into main (ADR-0001)")

    fields = ralph_story.validate_story(story).fields
    if fields.get("type") != "hil":
        errors.append(
            "type: --complete-hil only handles type:hil stories (got %s)"
            % (fields.get("type") or "none"))

    if errors:
        return Plan(False, errors, [], base=base)

    number = story["number"]
    branch = ralph_iterate.branch_name(story, branch_pattern)
    title = story.get("title") or ("Story #%s" % number)
    body = ("Refs #%s -- HIL story awaiting bench verification. "
            "See the story's ## Bench Test Procedure. Do not auto-close on merge."
            % number)
    commands = [
        ["git", "push", "-u", "origin", branch],
        ["gh", "pr", "create", "--base", base, "--head", branch,
         "--title", title, "--body", body],
        ["gh", "issue", "edit", str(number),
         "--add-label", AWAITING_BENCH_LABEL,
         "--remove-label", IN_PROGRESS_LABEL],
    ]
    return Plan(True, [], commands, base=base, branch=branch)


class CommandResult:
    def __init__(self, args, returncode, output):
        self.args = args
        self.returncode = returncode
        self.output = output
        self.ok = returncode == 0


class RunResult:
    def __init__(self, steps):
        self.steps = steps
        self.failed = next((s for s in steps if not s.ok), None)
        self.ok = self.failed is None


def run_plan(commands, cwd=None):
    """Execute plan commands (argv lists) in order, stopping at the first failure.

    Each command's combined stdout+stderr is captured (low-verbosity: the CLI
    surfaces output only for a failing command). Returns a RunResult.
    """
    results = []
    for args in commands:
        proc = subprocess.run(
            args, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        results.append(CommandResult(args, proc.returncode, proc.stdout))
        if proc.returncode != 0:
            break
    return RunResult(results)


def _load_story(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_complete(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --complete-hil requires a STORY path (or - for stdin)\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"

    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    branching = result.config["branching"]

    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    plan = hil_complete_plan(
        story, base=branching["base"],
        branch_pattern=branching["branch_pattern"])
    if not plan.ok:
        sys.stderr.write("REFUSED: hil completion\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_plan(plan.commands, cwd=os.getcwd())
    if run.ok:
        print("OK: opened PR for #%s to %s; moved to %s (awaiting bench)"
              % (story["number"], plan.base, AWAITING_BENCH_LABEL))
        return 0
    sys.stderr.write("FAILED: hil completion (exit %d): %s\n"
                     % (run.failed.returncode, " ".join(run.failed.args)))
    if run.failed.output.strip():
        sys.stderr.write(run.failed.output.rstrip() + "\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_hil.py complete <story.json | -> [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "complete":
        return _cmd_complete(rest)
    sys.stderr.write("ralph_hil.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
