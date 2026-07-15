"""HIL completion for the Ralph Loop (US-007, ADR-0001, ADR-0003, ADR-0006).

For a green `type:hil` story Ralph parks the issue at `state:awaiting-bench`
and STOPS -- it never merges a HIL story and never closes the issue; only a
human bench-verifying and closing it marks it Passing, so until then it does
not satisfy dependents' `Depends on:` edges (ADR-0002). How it parks branches
on Feature membership (ADR-0006): an Orphan Story (`Parent: None`) is pushed
to its story branch and PR'd to base so the human bench-tests one clean diff
in isolation; a Feature story (`Parent: #N`) is pushed to its Feature's
integration branch with the completion commit SHA recorded as a *bench anchor*
comment on the issue -- no PR -- so the human verifies at that exact commit
while sibling stories keep landing on the branch tip. Re-completion after
bench-fail rework appends a new anchor comment superseding the old (comments
are never edited). Ralph **never touches main** (ADR-0001).

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
    def __init__(self, ok, errors, commands, base=None, branch=None, anchor=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.branch = branch
        self.anchor = anchor


def hil_complete_plan(story, base=DEFAULT_BASE,
                      branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN,
                      prd=None,
                      feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN,
                      head_sha=None):
    """Build the ordered command plan to park a green HIL story at
    state:awaiting-bench.

    Pure: computes commands, runs nothing. The plan branches on Feature
    membership (ADR-0006): an Orphan Story (`Parent: None`) is pushed to its
    story branch and PR'd to base; a Feature story (`Parent: #N`) is pushed to
    its Feature's integration branch and `head_sha` -- the completion commit
    the caller resolved from HEAD -- is recorded as a bench anchor comment on
    the issue, with no PR. Each completion appends a fresh anchor comment, so
    re-completion after bench-fail rework supersedes the old anchor without
    editing history. Refuses (ok=False, no commands) when base is `main`
    (never touches main), the story is not `type:hil` (AFK auto-merge is
    US-006's job), a Feature story lacks its PRD context, or a Feature story
    lacks `head_sha`. Never emits a merge or a close: the human bench-verifies.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to open a PR into main (ADR-0001)")

    fields = ralph_story.validate_story(story).fields
    if fields.get("type") != "hil":
        errors.append(
            "type: --complete-hil only handles type:hil stories (got %s)"
            % (fields.get("type") or "none"))

    branch = None
    try:
        branch = ralph_iterate.resolve_branch(
            story, prd=prd, branch_pattern=branch_pattern,
            feature_pattern=feature_pattern)
    except ValueError as exc:
        errors.append("branch: %s" % exc)

    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is not None and not head_sha:
        errors.append(
            "head_sha: the completion commit SHA is required to record the "
            "bench anchor for a Feature story")

    if errors:
        return Plan(False, errors, [], base=base)

    number = story["number"]
    if parent is not None:
        # Feature story: push HEAD to the feature branch, record the bench
        # anchor, park at awaiting-bench. No PR -- the Feature's only PR is
        # feature/* -> base, opened by the completion pass. The human verifies
        # at the anchored commit, never at the moving branch tip.
        anchor_body = (
            "Bench anchor: %s\n\nBench-verify #%s at this exact commit "
            "(`git checkout %s`), not the branch tip -- sibling stories may "
            "land after it. Supersedes any earlier bench anchor on this "
            "story. See the story's ## Bench Test Procedure."
            % (head_sha, number, head_sha))
        commands = [
            ["git", "push", "-u", "origin", "HEAD:" + branch],
            ["gh", "issue", "comment", str(number), "--body", anchor_body],
            ["gh", "issue", "edit", str(number),
             "--add-label", AWAITING_BENCH_LABEL,
             "--remove-label", IN_PROGRESS_LABEL],
        ]
        return Plan(True, [], commands, base=base, branch=branch, anchor=head_sha)

    title = story.get("title") or ("Story #%s" % number)
    body = ("Refs #%s -- HIL story awaiting bench verification. "
            "See the story's ## Bench Test Procedure. Do not auto-close on merge."
            % number)
    commands = [
        # Push the iteration's current HEAD to the canonical remote branch. Using
        # HEAD: (not a bare local branch name) means promotion does not depend on
        # the local branch carrying the exact canonical name -- the iteration may
        # have named its checkout differently; whatever it committed to is HEAD.
        ["git", "push", "-u", "origin", "HEAD:" + branch],
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
    prd_path = rest[2] if len(rest) > 2 and rest[2] else None

    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    branching = result.config["branching"]

    try:
        story = _load_story(story_path)
        prd = _load_story(prd_path) if prd_path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    # A Feature story's bench anchor is the commit being completed: resolve
    # HEAD before the plan runs (the plan's push sends this same HEAD).
    head_sha = None
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is not None:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.getcwd(),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
        if proc.returncode != 0:
            sys.stderr.write("FAILED: hil completion: git rev-parse HEAD "
                             "(exit %d)\n" % proc.returncode)
            if proc.stdout.strip():
                sys.stderr.write(proc.stdout.rstrip() + "\n")
            return 1
        head_sha = proc.stdout.strip()

    plan = hil_complete_plan(
        story, base=branching["base"],
        branch_pattern=branching["branch_pattern"],
        prd=prd, feature_pattern=branching["feature_pattern"],
        head_sha=head_sha)
    if not plan.ok:
        sys.stderr.write("REFUSED: hil completion\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_plan(plan.commands, cwd=os.getcwd())
    if run.ok:
        if plan.anchor is not None:
            print("OK: pushed #%s to %s; bench anchor %s; moved to %s "
                  "(awaiting bench)"
                  % (story["number"], plan.branch, plan.anchor,
                     AWAITING_BENCH_LABEL))
        else:
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
