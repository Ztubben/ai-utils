"""Handoff checkpoint + resume for the Ralph Loop (US-008, ADR-0004).

Ralph never compacts context. When an iteration's context fills it writes a
**Handoff** and terminates cleanly; the next iteration resumes the same
`state:in-progress` story with clean context. A Handoff is durable state stored
only in the superproject:

  - an **issue comment** carrying a distinct handoff marker (so a context-full
    checkpoint is NOT counted as a failed Attempt -- an attempt counter excludes
    marked comments via `non_handoff_comments`), plus
  - **WIP commits** pushed to the story branch (`ralph/<issue#>-slug`).

Resume checks that branch back out. The base branch is never touched and `main`
is never touched (ADR-0001); resume state lives only in the superproject.

The deterministic seam mirrors AFK/HIL completion: pure command *plans*
(`handoff_plan` / `resume_plan`) return the ordered git/gh commands (argv lists)
without running anything, so the safety guards are unit-testable; `run_plan`
executes a plan fail-fast and the CLI wrapper (`ralph --checkpoint` /
`ralph --resume`) prints and sets exit codes.
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
IN_PROGRESS_STATE = "in-progress"
DEFAULT_BASE = "develop"

# Distinct marker every Handoff comment carries. A context-full checkpoint is not
# a failed Attempt (ADR-0004), so an attempt counter (US-009) excludes comments
# carrying this marker -- see `non_handoff_comments`.
HANDOFF_MARKER = "<!-- ralph:handoff -->"
CHECKPOINT_COMMIT_MSG = "WIP: Ralph handoff checkpoint (#%s)"


class Plan:
    def __init__(self, ok, errors, commands, base=None, branch=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.branch = branch


def _refuse_protected(base, branch, errors):
    """Guard the safety invariant: never touch base or main from a Handoff."""
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to checkpoint onto main (ADR-0001)")
    if (branch or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branch: refusing to touch main (ADR-0001)")


def handoff_plan(story, summary, base=DEFAULT_BASE,
                 branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN):
    """Build the ordered command plan to write a Handoff and terminate.

    Pure: computes commands, runs nothing. Stages + commits WIP, pushes the story
    branch, and posts an issue comment carrying `HANDOFF_MARKER` + the summary.
    The story stays `state:in-progress` so selection resumes it next iteration.
    Refuses (ok=False, no commands) when base is `main` (never touches main).
    """
    number = story["number"]
    branch = ralph_iterate.branch_name(story, branch_pattern)
    errors = []
    _refuse_protected(base, branch, errors)
    if errors:
        return Plan(False, errors, [], base=base, branch=branch)

    body = HANDOFF_MARKER + "\n\n" + (summary or "").strip()
    commands = [
        ["git", "add", "-A"],
        ["git", "commit", "--allow-empty", "-m", CHECKPOINT_COMMIT_MSG % number],
        ["git", "push", "-u", "origin", branch],
        ["gh", "issue", "comment", str(number), "--body", body],
    ]
    return Plan(True, [], commands, base=base, branch=branch)


def resume_plan(story, base=DEFAULT_BASE,
                branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN):
    """Build the plan to resume a checkpointed story: fetch + check out its branch.

    Pure. Refuses (ok=False, no commands) when the story is not
    `state:in-progress` (only a checkpointed story is resumed) or base is `main`.
    Never touches the base branch: it only fetches and checks out the story branch.
    """
    branch = ralph_iterate.branch_name(story, branch_pattern)
    fields = ralph_story.validate_story(story).fields
    errors = []
    if fields.get("state") != IN_PROGRESS_STATE:
        errors.append(
            "state: --resume only resumes a state:in-progress story (got %s)"
            % (fields.get("state") or "none"))
    _refuse_protected(base, branch, errors)
    if errors:
        return Plan(False, errors, [], base=base, branch=branch)

    commands = [
        ["git", "fetch", "origin"],
        ["git", "checkout", branch],
    ]
    return Plan(True, [], commands, base=base, branch=branch)


def _comment_body(comment):
    if isinstance(comment, str):
        return comment
    return comment.get("body", "") if isinstance(comment, dict) else ""


def is_handoff_comment(comment):
    """True if a comment (gh --json shape dict, or a plain string) is a Handoff."""
    return HANDOFF_MARKER in _comment_body(comment)


def non_handoff_comments(comments):
    """Comments that are NOT Handoff checkpoints. This is what an Attempt counter
    (US-009) operates on, so a context-full checkpoint never counts as an Attempt.
    """
    return [c for c in comments if not is_handoff_comment(c)]


def latest_handoff(comments):
    """The body of the most recent Handoff comment (gh lists comments oldest
    first), or None if the story has no Handoff yet."""
    handoffs = [c for c in comments if is_handoff_comment(c)]
    if not handoffs:
        return None
    return _comment_body(handoffs[-1])


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


def _load_config(config_path):
    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return None
    return result.config["branching"]


def _cmd_checkpoint(rest):
    if len(rest) < 2 or not rest[0] or rest[1] is None:
        sys.stderr.write(
            "ralph: --checkpoint requires STORY (path or -) and a SUMMARY\n")
        return 2
    story_path, summary = rest[0], rest[1]
    config_path = rest[2] if len(rest) > 2 and rest[2] else ".ralph.yml"

    branching = _load_config(config_path)
    if branching is None:
        return 2
    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    plan = handoff_plan(story, summary, base=branching["base"],
                        branch_pattern=branching["branch_pattern"])
    if not plan.ok:
        sys.stderr.write("REFUSED: handoff checkpoint\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_plan(plan.commands, cwd=os.getcwd())
    if run.ok:
        print("OK: checkpointed #%s onto %s (Handoff written; terminating)"
              % (story["number"], plan.branch))
        return 0
    sys.stderr.write("FAILED: handoff checkpoint (exit %d): %s\n"
                     % (run.failed.returncode, " ".join(run.failed.args)))
    if run.failed.output.strip():
        sys.stderr.write(run.failed.output.rstrip() + "\n")
    return 1


def _cmd_resume(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --resume requires a STORY path (or - for stdin)\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"

    branching = _load_config(config_path)
    if branching is None:
        return 2
    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    plan = resume_plan(story, base=branching["base"],
                       branch_pattern=branching["branch_pattern"])
    if not plan.ok:
        sys.stderr.write("REFUSED: resume\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_plan(plan.commands, cwd=os.getcwd())
    if not run.ok:
        sys.stderr.write("FAILED: resume (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1

    print("OK: resumed #%s on %s" % (story["number"], plan.branch))
    last = latest_handoff(story.get("comments", []))
    if last:
        print("--- last Handoff ---")
        print(last.rstrip())
    return 0


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage: ralph_handoff.py {checkpoint <story> <summary> | resume <story>} [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "checkpoint":
        return _cmd_checkpoint(rest)
    if mode == "resume":
        return _cmd_resume(rest)
    sys.stderr.write("ralph_handoff.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
