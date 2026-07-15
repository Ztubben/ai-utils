"""Feature completion pass for the Ralph Loop (US-028, ADR-0006).

When a Feature is complete (all stories closed, PRD open + state:ready), the
completion pass integrates the feature branch into base: (1) autosquash-collapse
fixup! commits into their story commits — on rebase conflict, abort and keep the
linear history (cosmetic fallback only); (2) rebase the cleaned feature branch
onto the current base branch; (3) run the full gating steps; (4) create the
Feature's single PR and merge it with a **merge commit** (never squash), then
close the PRD with a comment.

A rebase conflict in (2) or a red gate in (3) is a Feature-level blocker: push
the branch as-is, comment the details on the PRD, label it `ready-for-human`,
and stop — the `needs-human` circuit breaker is NOT tripped (independent features
don't poison each other).

The deterministic, host-testable seam mirrors AFK/HIL completion: a pure command
*plan* (`feature_complete_plan`) returns the ordered steps without running
anything, so the merge policy and main-safety guard are unit-testable.
`run_feature_plan` executes a plan step-by-step with fallback handling; the CLI
wrapper (`ralph --complete-feature`) prints and sets exit codes.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_iterate  # noqa: E402

PROTECTED_BRANCH = "main"
DEFAULT_BASE = "develop"
READY_FOR_HUMAN_LABEL = "ready-for-human"


class Plan:
    def __init__(self, ok, errors, steps, branch=None, base=None):
        self.ok = ok
        self.errors = errors
        self.steps = steps
        self.branch = branch
        self.base = base


class PlanResult:
    def __init__(self, ok, failed_step=None, on_fail_ran=False):
        self.ok = ok
        self.failed_step = failed_step
        self.on_fail_ran = on_fail_ran


def feature_complete_plan(prd, base=DEFAULT_BASE,
                          feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN,
                          gating=None):
    """Build the ordered step plan to integrate a complete Feature into base.

    Pure: computes steps, runs nothing. Each step is a dict with:
      - name: step identifier
      - commands: list of argv lists to run in order
      - fallback_ok (optional): if True, step failure is tolerated (cosmetic)
      - on_fail (optional): list of argv lists to run on failure (blocker path)

    Refuses (ok=False, no steps) when base is `main`.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to integrate into main (ADR-0001)")

    if errors:
        return Plan(False, errors, [], base=base)

    branch = ralph_iterate.branch_name(prd, feature_pattern)
    prd_number = prd["number"]

    # Blocker commands: push the branch, comment on PRD, label ready-for-human.
    # These are shared by the rebase and gating failure paths.
    def _blocker_commands(reason_placeholder):
        return [
            ["git", "push", "-u", "origin", "HEAD:" + branch],
            ["gh", "issue", "comment", str(prd_number),
             "--body", "Feature completion blocked: %s on `%s`. "
             "Branch pushed as-is for human triage." % (reason_placeholder, branch)],
            ["gh", "issue", "edit", str(prd_number),
             "--add-label", READY_FOR_HUMAN_LABEL],
        ]

    steps = [
        # Step 1: autosquash-collapse fixup! commits
        {
            "name": "autosquash",
            "commands": [
                ["git", "rebase", "--autosquash", base],
            ],
            "fallback_ok": True,  # conflict -> keep linear history, continue
            "abort_on_fail": [
                ["git", "rebase", "--abort"],
            ],
        },
        # Step 2: rebase onto current base
        {
            "name": "rebase",
            "commands": [
                ["git", "rebase", base],
            ],
            "on_fail": _blocker_commands("rebase conflict"),
            "abort_on_fail": [
                ["git", "rebase", "--abort"],
            ],
        },
        # Step 3: run full gating
        {
            "name": "gating",
            "commands": [],  # filled by the runner from the gating config
            "gating_steps": gating or [],
            "on_fail": _blocker_commands("gating failure"),
        },
        # Step 4: create PR and merge with merge commit
        {
            "name": "pr-create",
            "commands": [
                ["gh", "pr", "create", "--base", base, "--head", branch,
                 "--title", prd.get("title", "Feature #%d" % prd_number),
                 "--body", "Merges Feature #%d into %s.\n\nCloses #%d"
                 % (prd_number, base, prd_number)],
                ["gh", "pr", "merge", branch, "--merge", "--delete-branch"],
            ],
        },
        # Step 5: close the PRD
        {
            "name": "prd-close",
            "commands": [
                ["gh", "issue", "close", str(prd_number),
                 "--comment", "Feature integrated into %s via merge commit; "
                 "PRD closed." % base],
            ],
        },
    ]

    return Plan(True, [], steps, branch=branch, base=base)


def run_feature_plan(plan, cwd=None, dry_run=False, inject_failures=None):
    """Execute a feature completion plan step-by-step.

    Each step's commands are run in order. On failure:
      - If the step has `fallback_ok`, abort (if abort_on_fail is present) and
        continue to the next step.
      - If the step has `on_fail`, run those commands and return a failed result.
      - Otherwise, return a failed result immediately.

    dry_run: if True, don't actually execute anything (for plan-level testing).
    inject_failures: set of step names that should simulate failure (testing only).
    """
    inject_failures = inject_failures or set()

    for step in plan.steps:
        name = step["name"]
        failed = name in inject_failures

        if not dry_run and not failed:
            # For gating steps, run them via ralph_iterate.run_gating
            if name == "gating" and step.get("gating_steps"):
                gres = ralph_iterate.run_gating(step["gating_steps"], cwd=cwd)
                failed = not gres.ok
            else:
                for args in step["commands"]:
                    proc = subprocess.run(
                        args, cwd=cwd,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if proc.returncode != 0:
                        failed = True
                        break

        if failed:
            # Run abort commands if present (e.g. git rebase --abort)
            if step.get("abort_on_fail") and not dry_run:
                for args in step["abort_on_fail"]:
                    subprocess.run(args, cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            if step.get("fallback_ok"):
                # Cosmetic failure (autosquash): continue
                continue

            # Blocker: run on_fail commands and stop
            on_fail_ran = False
            if step.get("on_fail"):
                on_fail_ran = True
                if not dry_run:
                    for args in step["on_fail"]:
                        subprocess.run(args, cwd=cwd,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            return PlanResult(False, failed_step=name, on_fail_ran=on_fail_ran)

    return PlanResult(True)


def _load_prd(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_complete(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --complete-feature requires a PRD path (or - for stdin)\n")
        return 2
    prd_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"

    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    branching = result.config["branching"]
    gating = result.config["gating"]

    try:
        prd = _load_prd(prd_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read PRD: %s\n" % exc)
        return 2

    plan = feature_complete_plan(
        prd, base=branching["base"],
        feature_pattern=branching["feature_pattern"],
        gating=gating)
    if not plan.ok:
        sys.stderr.write("REFUSED: feature completion\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_feature_plan(plan, cwd=os.getcwd())
    if run.ok:
        print("OK: Feature #%s integrated into %s (merge commit); PRD closed"
              % (prd["number"], plan.base))
        return 0
    sys.stderr.write("BLOCKED: feature completion at step '%s'\n" % run.failed_step)
    if run.on_fail_ran:
        sys.stderr.write("  branch pushed, PRD commented + labeled ready-for-human\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_feature.py complete <prd.json | -> [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "complete":
        return _cmd_complete(rest)
    sys.stderr.write("ralph_feature.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
