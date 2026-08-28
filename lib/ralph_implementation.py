"""Promote locally green implementation work into model review (#49).

This is intentionally separate from final AFK/HIL completion.  Both Story
types first push their working branch, create (or update) a marked pull
request, and enter ``state:in-review``.  Nothing here merges, closes, or moves
a HIL Story to awaiting-bench.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_review  # noqa: E402
import ralph_story  # noqa: E402

PROTECTED_BRANCH = "main"
IN_PROGRESS_LABEL = "state:in-progress"
IN_REVIEW_LABEL = "state:in-review"
DEFAULT_BASE = "develop"


class Plan:
    def __init__(self, ok, errors, commands, base=None, branch=None,
                 pr_number=None, updated=False):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.branch = branch
        self.pr_number = pr_number
        self.updated = updated


def pull_request_body(number):
    """The durable, machine-detectable body for a Ralph-managed Story PR."""
    return (ralph_review.MANAGED_PR_MARKER + "\n\n"
            + "Refs #%s\n\n"
            + "Implementation is locally green; awaiting Ralph model review."
            ) % number


def implementation_green_plan(
        story, base=DEFAULT_BASE,
        branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN, prd=None,
        feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN,
        existing_pr=None):
    """Return the ordered push/PR/state plan for locally green work.

    ``existing_pr`` must be an already-open, marked PR for the resolved head.
    Supplying an unmarked PR is refused: it is outside the automated-review
    opt-in boundary.  The function is pure; live PR discovery belongs to the
    CLI wrapper.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to open a PR into main (ADR-0001)")

    validation = ralph_story.validate_story(story)
    fields = validation.fields
    if fields.get("type") not in ("afk", "hil"):
        errors.append("type: implementation-green requires type:afk or type:hil")
    if fields.get("state") not in ("in-progress", "in-review"):
        errors.append(
            "state: implementation-green requires state:in-progress or "
            "state:in-review (got %s)" % (fields.get("state") or "none"))

    branch = None
    try:
        branch = ralph_iterate.resolve_branch(
            story, prd=prd, branch_pattern=branch_pattern,
            feature_pattern=feature_pattern)
    except ValueError as exc:
        errors.append("branch: %s" % exc)

    if existing_pr is not None and not ralph_review.is_managed_pr(existing_pr):
        errors.append(
            "pull_request: open PR #%s is not Ralph-managed; refusing automated review"
            % existing_pr.get("number", "?"))

    if errors:
        return Plan(False, errors, [], base=base, branch=branch)

    number = story["number"]
    title = story.get("title") or ("Story #%s" % number)
    body = pull_request_body(number)
    commands = [["git", "push", "-u", "origin", "HEAD:" + branch]]
    if existing_pr is None:
        commands.append([
            "gh", "pr", "create", "--base", base, "--head", branch,
            "--title", title, "--body", body,
        ])
    else:
        commands.append([
            "gh", "pr", "edit", str(existing_pr["number"]),
            "--title", title, "--body", body,
        ])
    if fields.get("state") != "in-review":
        commands.append([
            "gh", "issue", "edit", str(number),
            "--add-label", IN_REVIEW_LABEL,
            "--remove-label", IN_PROGRESS_LABEL,
        ])
    return Plan(True, [], commands, base=base, branch=branch,
                pr_number=(existing_pr or {}).get("number"),
                updated=existing_pr is not None)


class CommandResult:
    def __init__(self, args, returncode, output):
        self.args = args
        self.returncode = returncode
        self.output = output
        self.ok = returncode == 0


class RunResult:
    def __init__(self, steps):
        self.steps = steps
        self.failed = next((step for step in steps if not step.ok), None)
        self.ok = self.failed is None


def run_plan(commands, cwd=None):
    results = []
    for args in commands:
        proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
        results.append(CommandResult(args, proc.returncode, proc.stdout))
        if proc.returncode != 0:
            break
    return RunResult(results)


def open_pull_requests(branch, cwd=None):
    """Read open PRs for a head branch; return ``(prs, error)``."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--state", "open", "--head", branch,
         "--json", "number,body"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        return None, proc.stdout.strip() or "gh pr list failed"
    try:
        prs = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        return None, "invalid gh pr list JSON: %s" % exc
    if not isinstance(prs, list):
        return None, "gh pr list returned a non-list result"
    return prs, None


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_green(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --implementation-green requires a STORY path (or -)\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"
    prd_path = rest[2] if len(rest) > 2 and rest[2] else None
    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for error in result.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    try:
        story = _load(story_path)
        prd = _load(prd_path) if prd_path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    branching = result.config["branching"]
    # Resolve once without an existing PR so discovery uses exactly the same
    # canonical branch as the eventual action plan.
    preview = implementation_green_plan(
        story, base=branching["base"],
        branch_pattern=branching["branch_pattern"], prd=prd,
        feature_pattern=branching["feature_pattern"])
    if not preview.ok:
        plan = preview
    else:
        prs, error = open_pull_requests(preview.branch, cwd=os.getcwd())
        if error:
            sys.stderr.write("FAILED: implementation-green PR discovery: %s\n" % error)
            return 1
        if len(prs) > 1:
            sys.stderr.write(
                "REFUSED: implementation-green\n  - pull_request: multiple open PRs for %s\n"
                % preview.branch)
            return 2
        existing = prs[0] if prs else None
        plan = implementation_green_plan(
            story, base=branching["base"],
            branch_pattern=branching["branch_pattern"], prd=prd,
            feature_pattern=branching["feature_pattern"],
            existing_pr=existing)

    if not plan.ok:
        sys.stderr.write("REFUSED: implementation-green\n")
        for error in plan.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    run = run_plan(plan.commands, cwd=os.getcwd())
    if not run.ok:
        sys.stderr.write("FAILED: implementation-green (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1
    verb = "updated PR #%s" % plan.pr_number if plan.updated else "opened PR"
    print("OK: %s for #%s; moved to %s" %
          (verb, story["number"], IN_REVIEW_LABEL))
    return 0


def main(argv):
    if argv and argv[0] == "green":
        return _cmd_green(argv[1:])
    sys.stderr.write("usage: ralph_implementation.py green STORY [CONFIG] [PRD]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
