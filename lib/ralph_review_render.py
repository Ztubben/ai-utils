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
import ralph_ledger  # noqa: E402
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


def review_body(result, prior_findings=None, usage_event=None):
    """The pull-request review body: what reviewed what, then the prose.

    Located findings are deliberately absent: they are rendered as inline
    threads, and repeating them here would double every objection.

    A later round also states, by identifier, what became of every finding the
    round before it raised.  A withdrawal is otherwise invisible -- the earlier
    thread just stops being mentioned -- and "the objection was dropped" is
    exactly what the author, and any human arbitrating later, needs to read.
    """
    lines = [
        # The stamp is what a later round reads to know this head is done; it
        # rides on the review body so the fact lives with the review itself.
        ralph_review.review_marker(result["head"]),
        "Ralph model review — round %s" % result["round"],
        "",
        "Reviewing model: `%s`" % result["model"],
        "Reviewed commit: %s" % result["head"],
        "",
        result["summary"],
    ]
    if prior_findings:
        verdict = ralph_review_result.adjudicate(prior_findings,
                                                 result.get("findings", []))
        lines += ["", "## Earlier findings", ""]
        lines += ["- **%s** — %s" % pair for pair in verdict.outcomes()]
    findings = cross_cutting(result)
    if findings:
        lines += ["", "## Cross-cutting findings", ""]
        lines.append("\n\n".join(_finding_block(f) for f in findings))
    footer = ralph_ledger.usage_footer(usage_event)
    if footer:
        # This round's own cost, on this round's own review (#63): the ledger
        # on the Story is the aggregate, and nobody reading a pull request
        # should have to open it to see what the review in front of them cost.
        lines += ["", "---", "", footer]
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


def review_payload(result, prior_findings=None, usage_event=None):
    """The exact JSON body for `POST /repos/{owner}/{repo}/pulls/{n}/reviews`."""
    return {
        "commit_id": result["head"],
        "event": REVIEW_EVENT,
        "body": review_body(result, prior_findings=prior_findings,
                            usage_event=usage_event),
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


def render_plan(result, pull_request, payload_path, story_number=None):
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
    if story_number is not None:
        # Keep the judgement machine-readable where a later round can recover it
        # exactly. Written after the review itself: a record of something that
        # was never posted would be a lie about the pull request's state.
        commands.append(["gh", "issue", "comment", str(story_number),
                         "--body", ralph_review.result_record(result)])
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


class PublishResult:
    """What one attempt to post a validated result produced.

    ``failed`` separates the two ways publishing does not happen: a refusal
    (nothing was ever sent) from a gh call that ran and returned non-zero.
    """

    def __init__(self, ok, errors, failed=None, posted_comments=0):
        self.ok = ok
        self.errors = errors
        self.failed = failed
        self.posted_comments = posted_comments


def publish(result, pull_request, cwd=None, story_number=None,
            prior_findings=None, usage_event=None):
    """Post *result* onto *pull_request* and update the required check.

    The nested ``comments[]`` body cannot be expressed as `gh api -f`, so the
    payload is written to a temp file the plan references by path; the plan
    itself stays a pure argv list.
    """
    payload = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="ralph-review-", delete=False)
    try:
        json.dump(review_payload(result, prior_findings=prior_findings,
                                 usage_event=usage_event), payload, indent=2)
        payload.close()
        plan = render_plan(result, pull_request, payload.name,
                           story_number=story_number)
        if not plan.ok:
            return PublishResult(False, plan.errors)
        run = run_plan(plan.commands, cwd=cwd)
    finally:
        os.unlink(payload.name)
    if not run.ok:
        return PublishResult(
            False, ["%s: exit %d" % (" ".join(run.failed.args),
                                     run.failed.returncode)],
            failed=run.failed)
    return PublishResult(True, [], posted_comments=plan.posted_comments)


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

    posted = publish(result, pull_request, cwd=os.getcwd())
    if not posted.ok:
        if posted.failed is None:
            sys.stderr.write("REFUSED: render-review\n")
            for error in posted.errors:
                sys.stderr.write("  - %s\n" % error)
            return 2
        sys.stderr.write("FAILED: render-review (exit %d): %s\n"
                         % (posted.failed.returncode,
                            " ".join(posted.failed.args)))
        if posted.failed.output.strip():
            sys.stderr.write(posted.failed.output.rstrip() + "\n")
        return 1
    print("OK: posted round %s review on PR #%s (%d inline thread%s); %s"
          % (result["round"], pull_request.get("number", "?"),
             posted.posted_comments,
             "" if posted.posted_comments == 1 else "s",
             check_description(result)))
    return 0


def main(argv):
    if argv and argv[0] == "render":
        return _cmd_render(argv[1:])
    sys.stderr.write("usage: ralph_review_render.py render REVIEW PR [DIFF]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
