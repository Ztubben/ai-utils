"""AFK completion for the Ralph Loop (US-006, ADR-0001).

For a green `type:afk` story Ralph auto-merges the story branch into base per the
configured `afk_merge` policy and closes the issue (marks it Passing), so
fully-autonomous work keeps `base` (default `develop`) moving and unblocks
dependents. Ralph integrates into base and **never touches main** (ADR-0001).

The deterministic, host-testable seam is a pure command *plan*: `afk_complete_plan`
returns the ordered git/gh commands (as argv lists) without running anything, so
the merge/close policy is unit-testable and the main-safety guard is provable.
`run_plan` executes a plan fail-fast against git/gh; the CLI wrapper
(`ralph --complete-afk`) prints and sets exit codes. A merged story ends up
closed, which is exactly what `ralph_select` treats as a satisfied dependency.
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
MERGE_FLAG = {"merge": "--merge", "squash": "--squash", "rebase": "--rebase"}
DEFAULT_BASE = "develop"
DEFAULT_AFK_MERGE = "squash"


class Plan:
    def __init__(self, ok, errors, commands, base=None, branch=None, method=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.branch = branch
        self.method = method


def afk_complete_plan(story, base=DEFAULT_BASE,
                      branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN,
                      afk_merge=DEFAULT_AFK_MERGE):
    """Build the ordered command plan to auto-merge a green AFK story + close it.

    Pure: computes commands, runs nothing. Refuses (ok=False, no commands) when
    base is `main`, the story is not `type:afk`, or afk_merge is unknown.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to auto-merge into main (ADR-0001)")

    fields = ralph_story.validate_story(story).fields
    if fields.get("type") != "afk":
        errors.append(
            "type: --complete-afk only handles type:afk stories (got %s)"
            % (fields.get("type") or "none"))

    if afk_merge not in MERGE_FLAG:
        errors.append("branching/afk_merge: unknown merge method %r" % afk_merge)

    if errors:
        return Plan(False, errors, [], base=base)

    number = story["number"]
    branch = ralph_iterate.branch_name(story, branch_pattern)
    title = story.get("title") or ("Story #%s" % number)
    commands = [
        # Push the iteration's current HEAD to the canonical remote branch. Using
        # HEAD: (not a bare local branch name) means promotion does not depend on
        # the local branch carrying the exact canonical name -- the iteration may
        # have named its checkout differently; whatever it committed to is HEAD.
        ["git", "push", "-u", "origin", "HEAD:" + branch],
        ["gh", "pr", "create", "--base", base, "--head", branch,
         "--title", title, "--body", "Closes #%s" % number],
        ["gh", "pr", "merge", branch, MERGE_FLAG[afk_merge], "--delete-branch"],
        ["gh", "issue", "close", str(number),
         "--comment", "Merged into %s and marked Passing (AFK)." % base],
    ]
    return Plan(True, [], commands, base=base, branch=branch, method=afk_merge)


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
        sys.stderr.write("ralph: --complete-afk requires a STORY path (or - for stdin)\n")
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

    plan = afk_complete_plan(
        story, base=branching["base"],
        branch_pattern=branching["branch_pattern"],
        afk_merge=branching["afk_merge"])
    if not plan.ok:
        sys.stderr.write("REFUSED: afk completion\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_plan(plan.commands, cwd=os.getcwd())
    if run.ok:
        print("OK: merged #%s into %s (%s); issue closed"
              % (story["number"], plan.base, plan.method))
        return 0
    sys.stderr.write("FAILED: afk completion (exit %d): %s\n"
                     % (run.failed.returncode, " ".join(run.failed.args)))
    if run.failed.output.strip():
        sys.stderr.write(run.failed.output.rstrip() + "\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_afk.py complete <story.json | -> [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "complete":
        return _cmd_complete(rest)
    sys.stderr.write("ralph_afk.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
