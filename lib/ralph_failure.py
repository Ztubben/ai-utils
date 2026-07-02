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
            "| check-breaker [backlog]} [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "record-attempt":
        return _cmd_record_attempt(rest)
    if mode == "check-breaker":
        return _cmd_check_breaker(rest)
    sys.stderr.write("ralph_failure.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
