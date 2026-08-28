"""Render a validated review result onto a pull request (#52).

The Review Agent holds no GitHub credential; this trusted wrapper is what turns
its validated result into ordinary review artifacts.
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_review  # noqa: E402
import ralph_review_result  # noqa: E402

# Every review is posted as a COMMENT.  GitHub refuses APPROVE and
# REQUEST_CHANGES on a pull request the same account authored, and Ralph's
# pull requests are opened with the operator's own credential; the required
# check below, not the review event, carries the authoritative verdict.
REVIEW_EVENT = "COMMENT"

# One stable context, so a target repository can require it in branch
# protection once and never track a per-round or per-model check name.
CHECK_CONTEXT = "ralph/model-review"


def cross_cutting(result):
    """Findings with no source location, which no inline thread can carry."""
    return [f for f in result.get("findings", []) if not f.get("location")]


def _finding_block(finding):
    """One finding as Markdown.

    The three supporting lines are a list, not consecutive lines: GitHub
    collapses single newlines into one paragraph, which would run evidence,
    requirement, and verification together.
    """
    return "\n".join([
        "**%s** (%s, %s)" % (finding["id"], finding["category"],
                             "blocking" if finding["blocking"] else "non-blocking"),
        "",
        finding["claim"],
        "",
        "- _Evidence_: %s" % finding["evidence"],
        "- _Requirement_: %s" % finding["requirement"],
        "- _Verify_: %s" % finding["verification"],
    ])


def review_body(result):
    """The pull-request review body: what reviewed what, then the prose.

    Located findings are deliberately absent: they are rendered as inline
    threads, and repeating them here would double every objection.
    """
    lines = [
        "Ralph model review — round %s" % result["round"],
        "",
        "Reviewing model: `%s`" % result["model"],
        "Reviewed commit: %s" % result["head"],
        "",
        result["summary"],
    ]
    findings = cross_cutting(result)
    if findings:
        lines += ["", "## Cross-cutting findings", ""]
        lines.append("\n\n".join(_finding_block(f) for f in findings))
    return "\n".join(lines)


def inline_comments(result):
    """One review thread per located finding, anchored on the new side.

    A range is anchored at its last line with ``start_line`` above it, which is
    how GitHub addresses a multi-line thread; a single-line finding carries no
    range at all, because sending ``start_line == line`` is rejected.
    """
    comments = []
    for finding in result.get("findings", []):
        location = finding.get("location")
        if not location:
            continue
        start = location["line"]
        end = location.get("end_line", start)
        comment = {"path": location["path"], "line": end, "side": "RIGHT",
                   "body": _finding_block(finding)}
        if end != start:
            comment["start_line"] = start
            comment["start_side"] = "RIGHT"
        comments.append(comment)
    return comments


def review_payload(result):
    """The exact JSON body for `POST /repos/{owner}/{repo}/pulls/{n}/reviews`."""
    return {
        "commit_id": result["head"],
        "event": REVIEW_EVENT,
        "body": review_body(result),
        "comments": inline_comments(result),
    }


class Plan:
    def __init__(self, ok, errors, commands, head=None, posted_comments=0):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.head = head
        self.posted_comments = posted_comments


def blocking_findings(result):
    return [f for f in result.get("findings", []) if f.get("blocking")]


def check_description(result):
    """The check's one line: which round, which model, and what it decided.

    GitHub truncates a status description at 140 characters, so the model
    identity is what gets trimmed if anything must be.
    """
    blockers = len(blocking_findings(result))
    line = "Round %s · %s · %d blocking finding%s" % (
        result["round"], result["model"], blockers, "" if blockers == 1 else "s")
    return line[:140]


def check_command(result):
    """Update the one required-check context with the authoritative verdict."""
    state = "failure" if result["verdict"] == "request_changes" else "success"
    return [
        "gh", "api", "--method", "POST",
        "repos/{owner}/{repo}/statuses/%s" % result["head"],
        "-f", "state=" + state,
        "-f", "context=" + CHECK_CONTEXT,
        "-f", "description=" + check_description(result),
    ]


def render_plan(result, pull_request, payload_path):
    """Return the ordered plan that renders *result* onto *pull_request*.

    A result is bound to the commit it judged.  If the pull request has moved
    on, the findings describe code that is no longer there, so nothing is
    posted and no check is written: a stale result is discarded, never
    presented as current.
    """
    errors = []
    if not ralph_review.is_managed_pr(pull_request):
        errors.append(
            "pull_request: #%s is not Ralph-managed; refusing to post automated "
            "review" % pull_request.get("number", "?"))
    current = pull_request.get("headRefOid")
    if current != result["head"]:
        errors.append(
            "head: review judged %s but the pull request head is now %s; "
            "discarding the stale result" % (result["head"], current))
    if errors:
        return Plan(False, errors, [], head=result["head"])

    commands = [
        ["gh", "api", "--method", "POST",
         "repos/{owner}/{repo}/pulls/%s/reviews" % pull_request["number"],
         "--input", payload_path],
        check_command(result),
    ]
    return Plan(True, [], commands, head=result["head"],
                posted_comments=len(inline_comments(result)))


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


def _read(path):
    if path == "-":
        return sys.stdin.read()
    with open(path) as fh:
        return fh.read()


def _cmd_render(rest):
    if not 2 <= len(rest) <= 3:
        sys.stderr.write("usage: ralph --render-review REVIEW PR [DIFF]\n")
        return 2
    review_path, pr_path = rest[0], rest[1]
    try:
        raw = _read(review_path)
        result = json.loads(raw)
        pull_request = json.loads(_read(pr_path))
        changed = (ralph_review_result.changed_lines(_read(rest[2]))
                   if len(rest) == 3 else None)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read review result: %s\n" % exc)
        return 2

    # The wrapper renders validated output only: this is the same gate
    # `--validate-review` is, applied where the posting actually happens.
    validation = ralph_review_result.validate_review(result, changed=changed, raw=raw)
    if not validation.ok:
        sys.stderr.write("REFUSED: render-review (invalid review result)\n")
        for error in validation.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    payload = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="ralph-review-", delete=False)
    try:
        json.dump(review_payload(result), payload, indent=2)
        payload.close()
        plan = render_plan(result, pull_request, payload.name)
        if not plan.ok:
            sys.stderr.write("REFUSED: render-review\n")
            for error in plan.errors:
                sys.stderr.write("  - %s\n" % error)
            return 2
        run = run_plan(plan.commands, cwd=os.getcwd())
    finally:
        os.unlink(payload.name)

    if not run.ok:
        sys.stderr.write("FAILED: render-review (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1
    print("OK: posted round %s review on PR #%s (%d inline thread%s); %s"
          % (result["round"], pull_request.get("number", "?"),
             plan.posted_comments, "" if plan.posted_comments == 1 else "s",
             check_description(result)))
    return 0


def main(argv):
    if argv and argv[0] == "render":
        return _cmd_render(argv[1:])
    sys.stderr.write("usage: ralph_review_render.py render REVIEW PR [DIFF]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
