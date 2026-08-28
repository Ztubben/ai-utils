"""Hand a deadlocked negotiation to a human (#57).

Two Negotiation Rounds that settle nothing are the end of what two models can
usefully do to each other.  What happens then is deliberately narrow: *this*
Story moves to `state:blocked` and a native GitHub review is requested from the
configured human, with both sides' arguments summarized on the pull request so
the human can arbitrate without reading the whole thread.

It is emphatically not the global halt.  Unrelated Stories stay selectable, and
whether the loop as a whole should stop remains the circuit breaker's decision,
made from how many Stories are blocked -- which this one now counts toward.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_round  # noqa: E402

BLOCKED_LABEL = "state:blocked"
IN_REVIEW_LABEL = "state:in-review"


class Unsettled:
    """One finding the negotiation never closed, and every answer to it."""

    def __init__(self, finding, answers):
        self.finding = finding
        self.answers = answers


def unsettled(comments):
    """The blocking findings still open, each with both sides' arguments.

    The last round's blockers are what is open: a finding the reviewer withdrew
    simply is not there any more, and one whose fix satisfied the reviewer is
    not there either.  Non-blocking remarks never deadlock anything -- they were
    always the author's to take or leave.

    Every answer the finding ever received is carried, not just the last one.
    A human arbitrating wants to see the argument develop; a dispute repeated
    across two rounds reads very differently from one raised late.
    """
    history = ralph_review.negotiation_history(comments)
    reviews = [entry["payload"] for entry in history if entry["kind"] == "review"]
    answers = {}
    for entry in history:
        if entry["kind"] != "response":
            continue
        for disposition in entry["payload"].get("dispositions") or []:
            # The round travels with the answer: it is the difference between
            # an argument that held up and one made once and dropped.
            answers.setdefault(disposition.get("id"), []).append(
                dict(disposition, round=entry["payload"].get("round")))
    latest = reviews[-1] if reviews else {}
    return [Unsettled(f, answers.get(f.get("id"), []))
            for f in latest.get("findings") or [] if f.get("blocking")]


def _argument(item):
    lines = ["### %s (%s)" % (item.finding["id"], item.finding["category"]), ""]
    lines += ["**The Review Agent's case**", "",
              item.finding["claim"], "",
              "- _Evidence_: %s" % item.finding["evidence"],
              "- _Requirement_: %s" % item.finding["requirement"],
              "- _Verify_: %s" % item.finding["verification"], ""]
    if not item.answers:
        lines += ["**The Implementation Agent's answer**", "",
                  "None on the record.", ""]
        return "\n".join(lines)
    lines += ["**The Implementation Agent's answer**", ""]
    for answer in item.answers:
        lines.append("- Round %s — **%s**: %s"
                     % (answer.get("round", "?"), answer["disposition"],
                        answer["note"]))
        if answer.get("evidence"):
            lines.append("  - _Evidence_: %s" % answer["evidence"])
    lines.append("")
    return "\n".join(lines)


def escalation_comment(story, items, rounds, handle):
    """The one comment a human needs in order to settle this.

    Both cases in full, side by side, on the pull request the human is being
    asked to review -- not a pointer back into the thread history. Arbitration
    that requires reconstructing the argument first is arbitration that does
    not happen.
    """
    lines = [
        "## Model review deadlocked — @%s, over to you" % handle,
        "",
        "Story #%s and its %d Negotiation Round%s ended without agreement, so "
        "automated negotiation stops here. The Story is now `state:blocked`; "
        "unrelated Stories keep running."
        % (story.get("number", "?"), rounds, "" if rounds == 1 else "s"),
        "",
        "Settle it with an ordinary GitHub review: **Approve** releases the "
        "model-review gate even with these findings open, and **Request "
        "changes** sends the Story back to the implementation model with your "
        "feedback.",
        "",
        "## Unsettled findings", "",
    ]
    lines += [_argument(item) for item in items]
    return "\n".join(lines).rstrip() + "\n"


def escalate_plan(story, pull_request, items, rounds, handle):
    """The ordered plan that hands one deadlocked Story to a human.

    Pure: computes commands, runs nothing.  The order is the point -- the
    argument is on the pull request before the human is asked to read it, and
    the Story is only labelled blocked once the request that justifies the
    label has actually been made.
    """
    errors = []
    if not ralph_review.is_managed_pr(pull_request):
        errors.append(
            "pull_request: #%s is not Ralph-managed; refusing to escalate"
            % pull_request.get("number", "?"))
    if not items:
        errors.append("findings: nothing is unsettled; there is no deadlock "
                      "to escalate")
    if not handle:
        errors.append("notify/github: no handle to request a review from")
    if errors:
        return ralph_review_render.Plan(False, errors, [])

    number = pull_request["number"]
    return ralph_review_render.Plan(True, [], [
        ["gh", "pr", "comment", str(number), "--body",
         escalation_comment(story, items, rounds, handle)],
        ["gh", "pr", "edit", str(number), "--add-reviewer", handle],
        ["gh", "issue", "edit", str(story["number"]),
         "--add-label", BLOCKED_LABEL, "--remove-label", IN_REVIEW_LABEL],
    ])


def escalate(story, pull_request, config, root, comments=None):
    """Run the escalation against a live checkout; return an exit code."""
    if comments is None:
        try:
            comments = ralph_review_round.story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return 2
    handle = ((config or {}).get("notify") or {}).get("github")
    rounds = len(ralph_review.review_stamps(pull_request))
    plan = escalate_plan(story, pull_request, unsettled(comments), rounds,
                         handle)
    if not plan.ok:
        sys.stderr.write("REFUSED: escalate-review\n")
        for error in plan.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    run = ralph_review_render.run_plan(plan.commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: escalate-review (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1
    print("OK: #%s is blocked after %d round%s; @%s was asked to review PR #%s"
          % (story["number"], rounds, "" if rounds == 1 else "s", handle,
             pull_request.get("number", "?")))
    return 0


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_escalate(rest):
    args, pr_path = [], None
    i = 0
    while i < len(rest):
        if rest[i] == "--pr":
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: --pr requires a PATH\n")
                return 2
            i += 1
            pr_path = rest[i]
        elif rest[i].startswith("--"):
            sys.stderr.write("ralph: unknown option: %s\n" % rest[i])
            return 2
        else:
            args.append(rest[i])
        i += 1
    if not 1 <= len(args) <= 3:
        sys.stderr.write(
            "usage: ralph --escalate-review STORY [CONFIG] [ROOT] [--pr PATH]\n")
        return 2
    config_path = args[1] if len(args) > 1 and args[1] else ".ralph.yml"
    root = os.path.abspath(args[2] if len(args) > 2 and args[2] else os.getcwd())

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for error in validated.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    try:
        story = _load(args[0])
        if pr_path:
            pull_request, errors = _load(pr_path), []
        else:
            pull_request, errors = ralph_review_round.discover_pull_request(
                story, cwd=root)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not read the escalation's inputs: %s\n" % exc)
        return 2
    if pull_request is None:
        sys.stderr.write("REFUSED: escalate-review\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    return escalate(story, pull_request, validated.config, root)


def main(argv):
    if argv and argv[0] == "escalate":
        return _cmd_escalate(argv[1:])
    sys.stderr.write("usage: ralph_review_deadlock.py escalate STORY [CONFIG] "
                     "[ROOT] [--pr PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
