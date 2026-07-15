"""Failure handling + circuit breaker + needs-human for the Ralph Loop (US-009, ADR-0004).

An **Attempt** is an iteration that ends without the story reaching green (gating
red, or Ralph stuck). After `limits.max_attempts` (default 3) failed Attempts a
story moves to `state:blocked` with one terse comment, and Ralph continues with
other non-blocked eligible work. A context-full checkpoint (Handoff) is NOT an
Attempt -- so attempt counting is built on `ralph_handoff.non_handoff_comments`,
excluding checkpoints, and every real Attempt is recorded as an issue comment
carrying a distinct `ATTEMPT_MARKER`.

When a second story also blocks, `limits.circuit_breaker` (default 2) trips: the
loop halts, the `needs-human` label is applied to the most recently blocked story
and the configured GitHub handle is tagged in a comment. `ralph_select` treats a
`needs-human` label anywhere in the open backlog as a HALT, so applying the label
is exactly what stops the loop.

The deterministic seams mirror the completion stages (AFK/HIL/Handoff): pure
command *plans* (`attempt_plan` / `circuit_breaker_plan`) return the ordered gh
commands (argv lists) without running anything, so the counting and thresholds are
unit-testable; `run_plan` executes a plan fail-fast and the CLI wrapper
(`ralph --record-attempt` / `ralph --check-breaker`) prints and sets exit codes.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_handoff  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_select  # noqa: E402
import ralph_story  # noqa: E402

NEEDS_HUMAN_LABEL = "needs-human"
BLOCKED_STATE = "blocked"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_CIRCUIT_BREAKER = 2

# Distinct marker every failed-Attempt comment carries. Because it differs from the
# Handoff marker, a context-full checkpoint is never mistaken for an Attempt; the
# counter also filters checkpoints out explicitly via non_handoff_comments.
ATTEMPT_MARKER = "<!-- ralph:attempt -->"
NEEDS_HUMAN_MARKER = "<!-- ralph:needs-human -->"
BOUNDARY_MARKER = "<!-- ralph:boundary -->"


class Plan:
    def __init__(self, ok, errors, commands, **extra):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.blocked = extra.get("blocked", False)
        self.attempt_no = extra.get("attempt_no")
        self.max_attempts = extra.get("max_attempts")
        self.tripped = extra.get("tripped", False)
        self.target = extra.get("target")


def is_attempt_comment(comment):
    """True if a comment (gh --json shape dict, or a plain string) records an Attempt."""
    return ATTEMPT_MARKER in ralph_handoff._comment_body(comment)


def count_attempts(comments):
    """Number of failed Attempts recorded on a story.

    Built on `ralph_handoff.non_handoff_comments` so a context-full checkpoint is
    excluded first (ADR-0004: a checkpoint is not an Attempt); of what remains, the
    ones carrying `ATTEMPT_MARKER` are the Attempts.
    """
    considered = ralph_handoff.non_handoff_comments(comments or [])
    return sum(1 for c in considered if is_attempt_comment(c))


def attempt_plan(story, reason, max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Build the plan to record a failed Attempt, blocking the story at the limit.

    Pure: computes commands, runs nothing. Always posts one terse comment (carrying
    `ATTEMPT_MARKER` + the reason). When this Attempt reaches `max_attempts`, it also
    moves the story to `state:blocked` (adding state:blocked, removing its current
    state label). `plan.attempt_no`/`plan.blocked` report the count and whether the
    story was blocked. Never references `main`.
    """
    number = story["number"]
    attempt_no = count_attempts(story.get("comments", [])) + 1
    will_block = attempt_no >= max_attempts
    reason_txt = (reason or "").strip() or "no reason given"

    if will_block:
        body = "%s\n\nAttempt %d/%d failed -- moving to state:blocked: %s" % (
            ATTEMPT_MARKER, attempt_no, max_attempts, reason_txt)
    else:
        body = "%s\n\nAttempt %d/%d failed: %s" % (
            ATTEMPT_MARKER, attempt_no, max_attempts, reason_txt)

    commands = [["gh", "issue", "comment", str(number), "--body", body]]

    if will_block:
        current = ralph_story.validate_story(story).fields.get("state")
        edit = ["gh", "issue", "edit", str(number),
                "--add-label", "state:" + BLOCKED_STATE]
        if current and current != BLOCKED_STATE:
            edit += ["--remove-label", "state:" + current]
        commands.append(edit)

    return Plan(True, [], commands, blocked=will_block, attempt_no=attempt_no,
                max_attempts=max_attempts)


def _parse_boundary_sha(comments):
    """Extract the most recent boundary SHA from a story's comments.

    Returns the SHA string or None if no boundary comment exists.
    """
    sha = None
    for c in (comments or []):
        body = ralph_handoff._comment_body(c)
        if BOUNDARY_MARKER not in body:
            continue
        for line in body.splitlines():
            if line.startswith("Feature-branch boundary: "):
                sha = line.split(": ", 1)[1].strip()
    return sha


def boundary_plan(story, head_sha, prd=None):
    """Build the plan to record the feature-branch boundary SHA at story start.

    Pure: computes commands, runs nothing. For an Orphan Story (Parent: None)
    there is nothing to record (no feature branch to rewind), so the plan is
    empty but ok. For a Feature story the plan posts a boundary comment on the
    issue carrying `BOUNDARY_MARKER` and the current feature-branch HEAD SHA,
    so `reset_on_block_plan` can later find the rewind target.
    """
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is None:
        return Plan(True, [], [])

    errors = []
    if prd is None:
        errors.append(
            "prd: a Feature story requires its PRD issue to record a boundary")
    if not head_sha:
        errors.append(
            "head_sha: the feature-branch HEAD SHA is required to record the boundary")
    if errors:
        return Plan(False, errors, [])

    number = story["number"]
    body = ("%s\n\nFeature-branch boundary: %s" % (BOUNDARY_MARKER, head_sha))
    commands = [
        ["gh", "issue", "comment", str(number), "--body", body],
    ]
    return Plan(True, [], commands)


def reset_on_block_plan(story, reason, prd=None,
                        rescue_pattern="rescue/{issue}-{slug}",
                        feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN):
    """Build the plan to quarantine a blocked Feature story on a rescue branch.

    Pure: computes commands, runs nothing. For an Orphan Story (Parent: None)
    there is no feature branch to protect, so the plan is empty but ok (the
    caller uses the existing `attempt_plan` demotion unchanged). For a Feature
    story the plan:
      1. pushes the current HEAD to a rescue branch (named from rescue_pattern),
      2. force-pushes the feature branch back to the recorded boundary SHA,
      3. posts a demotion comment naming the rescue branch and the reason,
      4. relabels the story to state:blocked.
    Refuses when a Feature story lacks its PRD context or has no recorded
    boundary SHA.
    """
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is None:
        return Plan(True, [], [])

    errors = []
    if prd is None:
        errors.append(
            "prd: a Feature story requires its PRD issue for reset-on-block")

    boundary_sha = _parse_boundary_sha(story.get("comments"))
    if not boundary_sha:
        errors.append(
            "boundary: no boundary SHA recorded on #%s; cannot rewind"
            % story.get("number"))

    if errors:
        return Plan(False, errors, [])

    number = story["number"]
    rescue_branch = ralph_iterate.branch_name(story, rescue_pattern)
    feature_branch = ralph_iterate.branch_name(prd, feature_pattern)
    reason_txt = (reason or "").strip() or "no reason given"

    body = ("%s\n\nReset-on-block: demoting #%s to state:blocked.\n\n"
            "Reason: %s\n\n"
            "The story's work has been preserved on `%s`. "
            "The feature branch `%s` has been rewound to the boundary (%s)."
            % (ATTEMPT_MARKER, number, reason_txt,
               rescue_branch, feature_branch, boundary_sha))

    current = ralph_story.validate_story(story).fields.get("state")
    edit = ["gh", "issue", "edit", str(number),
            "--add-label", "state:" + BLOCKED_STATE]
    if current and current != BLOCKED_STATE:
        edit += ["--remove-label", "state:" + current]

    commands = [
        ["git", "push", "origin", "HEAD:refs/heads/" + rescue_branch],
        ["git", "push", "--force", "origin",
         boundary_sha + ":refs/heads/" + feature_branch],
        ["gh", "issue", "comment", str(number), "--body", body],
        edit,
    ]
    return Plan(True, [], commands)


def circuit_breaker_plan(raw_backlog, github_handle,
                         circuit_breaker=DEFAULT_CIRCUIT_BREAKER):
    """Build the plan to trip the circuit breaker, if enough stories are blocked.

    Pure. Normalizes the raw gh backlog and counts open `state:blocked` stories;
    when that count reaches `circuit_breaker`, it halts the loop by applying the
    `needs-human` label to the most recently blocked story (highest issue number)
    and tagging `github_handle` in a comment. `ralph_select` treats needs-human
    anywhere in the open backlog as a HALT. Emits no commands when not tripped.
    """
    stories = ralph_select.normalize(raw_backlog)
    blocked = [s for s in stories
               if not s.get("closed") and s.get("state") == BLOCKED_STATE
               and not s.get("is_blocker")]

    if len(blocked) < circuit_breaker:
        return Plan(True, [], [], tripped=False)

    target = max(blocked, key=lambda s: s["number"] if s["number"] is not None else -1)
    number = target["number"]
    handle = (github_handle or "").lstrip("@")
    body = ("%s\n\n@%s the Ralph Loop halted: %d stories are state:blocked "
            "(circuit breaker %d). Something systemic is likely wrong -- please "
            "investigate and reset." % (NEEDS_HUMAN_MARKER, handle, len(blocked),
                                        circuit_breaker))
    commands = [
        ["gh", "issue", "edit", str(number), "--add-label", NEEDS_HUMAN_LABEL],
        ["gh", "issue", "comment", str(number), "--body", body],
    ]
    return Plan(True, [], commands, tripped=True, target=number)


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


def _load_json(path):
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
    return result.config


def _report_run(run, ok_msg):
    if run.ok:
        print(ok_msg)
        return 0
    sys.stderr.write("FAILED (exit %d): %s\n"
                     % (run.failed.returncode, " ".join(run.failed.args)))
    if run.failed.output.strip():
        sys.stderr.write(run.failed.output.rstrip() + "\n")
    return 1


def _cmd_record_attempt(rest):
    if len(rest) < 2 or not rest[0] or rest[1] is None:
        sys.stderr.write(
            "ralph: --record-attempt requires STORY (path or -) and a REASON\n")
        return 2
    story_path, reason = rest[0], rest[1]
    config_path = rest[2] if len(rest) > 2 and rest[2] else ".ralph.yml"

    config = _load_config(config_path)
    if config is None:
        return 2
    try:
        story = _load_json(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    plan = attempt_plan(story, reason,
                        max_attempts=config["limits"]["max_attempts"])
    run = run_plan(plan.commands, cwd=os.getcwd())
    state = "blocked" if plan.blocked else "still workable"
    return _report_run(
        run, "OK: recorded Attempt %d/%d for #%s (%s)"
        % (plan.attempt_no, plan.max_attempts, story["number"], state))


def _cmd_record_boundary(rest):
    # Args: STORY [CONFIG] [PRD] SHA
    # SHA is always the last positional argument.
    if len(rest) < 2 or not rest[0]:
        sys.stderr.write(
            "ralph: --record-boundary requires STORY (path or -) and SHA\n")
        return 2
    story_path = rest[0]
    head_sha = rest[-1]
    # middle args: config and optional prd
    middle = rest[1:-1]  # between story and sha
    config_path = middle[0] if middle else ".ralph.yml"
    prd_path = middle[1] if len(middle) > 1 else None

    config = _load_config(config_path)
    if config is None:
        return 2
    try:
        story = _load_json(story_path)
        prd = _load_json(prd_path) if prd_path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    plan = boundary_plan(story, head_sha, prd=prd)
    if not plan.ok:
        sys.stderr.write("REFUSED: record-boundary\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    if not plan.commands:
        print("OK: orphan story; no boundary to record")
        return 0
    run = run_plan(plan.commands, cwd=os.getcwd())
    return _report_run(run, "OK: boundary recorded for #%s" % story["number"])


def _cmd_reset_on_block(rest):
    if len(rest) < 2 or not rest[0] or rest[1] is None:
        sys.stderr.write(
            "ralph: --reset-on-block requires STORY (path or -) and a REASON\n")
        return 2
    story_path, reason = rest[0], rest[1]
    config_path = rest[2] if len(rest) > 2 and rest[2] else ".ralph.yml"
    prd_path = rest[3] if len(rest) > 3 and rest[3] else None

    config = _load_config(config_path)
    if config is None:
        return 2
    try:
        story = _load_json(story_path)
        prd = _load_json(prd_path) if prd_path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    branching = config["branching"]
    plan = reset_on_block_plan(
        story, reason, prd=prd,
        rescue_pattern=branching["rescue_pattern"],
        feature_pattern=branching["feature_pattern"])
    if not plan.ok:
        sys.stderr.write("REFUSED: reset-on-block\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    if not plan.commands:
        print("OK: orphan story; no reset needed")
        return 0
    run = run_plan(plan.commands, cwd=os.getcwd())
    return _report_run(run, "OK: reset-on-block for #%s" % story["number"])


def _cmd_check_breaker(rest):
    backlog_path = rest[0] if rest and rest[0] else None
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"

    config = _load_config(config_path)
    if config is None:
        return 2
    try:
        backlog = _load_json(backlog_path) if backlog_path else ralph_select._scan_gh()
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read backlog: %s\n" % exc)
        return 2
    except subprocess.CalledProcessError as exc:
        sys.stderr.write("ralph: gh scan failed: %s\n" % exc)
        return 2

    plan = circuit_breaker_plan(
        backlog, config["notify"]["github"],
        circuit_breaker=config["limits"]["circuit_breaker"])
    if not plan.tripped:
        print("OK: circuit breaker not tripped")
        return 0
    run = run_plan(plan.commands, cwd=os.getcwd())
    return _report_run(
        run, "HALT: circuit breaker tripped -- needs-human on #%s, tagged @%s"
        % (plan.target, config["notify"]["github"].lstrip("@")))


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage: ralph_failure.py {record-attempt <story> <reason> "
            "| record-boundary <story> [config] [prd] <sha> "
            "| reset-on-block <story> <reason> [config] [prd] "
            "| check-breaker [backlog]} [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "record-attempt":
        return _cmd_record_attempt(rest)
    if mode == "record-boundary":
        return _cmd_record_boundary(rest)
    if mode == "reset-on-block":
        return _cmd_reset_on_block(rest)
    if mode == "check-breaker":
        return _cmd_check_breaker(rest)
    sys.stderr.write("ralph_failure.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
