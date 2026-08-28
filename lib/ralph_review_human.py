"""Human arbitration through GitHub's own review controls (#58).

There are no custom comment commands to learn.  A human settles a Story the way
they would settle any pull request: **Approve** or **Request changes**.

Approve is authoritative and releases the model-review gate even while model
findings remain unresolved -- a model never holds authority over a human
decision -- and the override is recorded on the Story so the audit trail says
what was overridden and by whom.  Request changes is authoritative feedback:
the Story goes back into review and the assigned implementation model is
launched with the human's own words.  An ordinary comment is just a comment.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402
import ralph_review_deadlock  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_respond  # noqa: E402
import ralph_review_round  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARBITRATION_PROMPT = os.path.join(REPO_ROOT, "prompts", "arbitration.v1.md")

# The two GitHub review states that carry authority. COMMENTED does not: an
# ordinary remark enriches the record and changes no machine state.
APPROVED = "APPROVED"
CHANGES_REQUESTED = "CHANGES_REQUESTED"
DECISIVE = (APPROVED, CHANGES_REQUESTED)

# The same one context the model review writes, because it is the same gate.
CHECK_CONTEXT = ralph_review_render.CHECK_CONTEXT

IN_REVIEW_LABEL = ralph_review_deadlock.IN_REVIEW_LABEL
BLOCKED_LABEL = ralph_review_deadlock.BLOCKED_LABEL


class Decision:
    """One authoritative human review."""

    def __init__(self, id, state, author, body, submitted_at=None):
        self.id = id
        self.state = state
        self.author = author
        self.body = body or ""
        self.submitted_at = submitted_at

    def approves(self):
        return self.state == APPROVED


def human_decision(pull_request):
    """The latest authoritative human review on this pull request, or None.

    Ralph's own reviews are excluded by the durable marker it stamps them with,
    not by author or by event type: the account Ralph posts as is the
    operator's, so "who wrote it" cannot tell them apart.
    """
    latest = None
    for review in (pull_request or {}).get("reviews") or []:
        body = (review or {}).get("body") or ""
        if ralph_review.REVIEW_MARKER_TEMPLATE.split("%s")[0] in body:
            continue
        if (review or {}).get("state") not in DECISIVE:
            continue
        author = (review.get("author") or review.get("user") or {})
        latest = Decision(review.get("id"), review["state"],
                          author.get("login") if isinstance(author, dict) else author,
                          body, review.get("submittedAt"))
    return latest


def approval_for(comments, head):
    """The recorded human approval covering *head*, if there is one.

    Bound to the commit it approved: an approval of an earlier head says
    nothing about work pushed after it, and treating it as blanket permission
    would let anything merge behind one click.
    """
    for record in ralph_review.arbitrations(comments):
        if record.get("decision") == APPROVED and record.get("head") == head:
            return record
    return None


def _label_command(story):
    """Return the Story to In Review, clearing any escalation it carried."""
    return ["gh", "issue", "edit", str(story["number"]),
            "--add-label", IN_REVIEW_LABEL, "--remove-label", BLOCKED_LABEL]


def _release_check(head, decision):
    """Release the one required check on the authority of a human."""
    description = ("Approved by @%s — human decision over model review"
                   % decision.author)[:140]
    return ["gh", "api", "--method", "POST",
            "repos/{owner}/{repo}/statuses/%s" % head,
            "-f", "state=success",
            "-f", "context=" + CHECK_CONTEXT,
            "-f", "description=" + description]


def approval_plan(story, pull_request, decision, comments):
    """The ordered plan that lets a human approval stand over model findings.

    The check is released first, because that is the decision taking effect;
    the label and the record follow.  What the approval overrode is written
    down by identifier -- an approval that silently buried two open blockers
    would leave nothing to audit later.
    """
    if not ralph_review.is_managed_pr(pull_request):
        return ralph_review_render.Plan(
            False, ["pull_request: #%s is not Ralph-managed"
                    % pull_request.get("number", "?")], [])
    head = pull_request.get("headRefOid")
    overrode = [item.finding["id"]
                for item in ralph_review_deadlock.unsettled(comments)]
    record = ralph_review.arbitration_record({
        "review": decision.id, "decision": decision.state,
        "reviewer": decision.author, "head": head, "overrode": overrode,
        "note": decision.body})
    return ralph_review_render.Plan(True, [], [
        _release_check(head, decision),
        _label_command(story),
        ["gh", "issue", "comment", str(story["number"]), "--body", record],
    ], head=head)


def reopen_plan(story, decision):
    """Return a Story to In Review on a human's Request changes."""
    if decision is None or decision.approves():
        return ralph_review_render.Plan(
            False, ["decision: reopening needs a human Request changes"], [])
    return ralph_review_render.Plan(True, [], [_label_command(story)])


def arbitration_record_command(story, pull_request, decision):
    """Write down that this native review has been acted on."""
    return ["gh", "issue", "comment", str(story["number"]), "--body",
            ralph_review.arbitration_record({
                "review": decision.id, "decision": decision.state,
                "reviewer": decision.author,
                "head": pull_request.get("headRefOid"),
                "overrode": [], "note": decision.body})]


def arbitration_prompt(decision, context, prompt_path=ARBITRATION_PROMPT):
    """The instructions, the human's own words, then the evidence.

    Verbatim, never summarized: a paraphrase of the one instruction that
    outranks every model in the loop is the last thing this should introduce.
    """
    with open(prompt_path) as fh:
        instructions = fh.read().rstrip()
    return ("%s\n\n---\n\n## The human's requested changes (@%s)\n\n%s\n\n"
            "---\n\n%s" % (instructions, decision.author,
                           decision.body.strip() or "(no message given)",
                           context))


def arbitrate(story, pull_request, config, root, comments=None):
    """Act on the human's decision, if there is a new one; return an exit code."""
    decision = human_decision(pull_request)
    if decision is None:
        print("OK: no human decision on PR #%s; nothing to arbitrate"
              % pull_request.get("number", "?"))
        return 0
    if comments is None:
        try:
            comments = ralph_review_round.story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return 2
    if ralph_review.arbitrated(comments, decision.id):
        print("OK: review %s by @%s is already acted on"
              % (decision.id, decision.author))
        return 0
    if decision.approves():
        return _apply_approval(story, pull_request, decision, comments, root)
    return _apply_requested_changes(story, pull_request, decision, config, root,
                                    comments)


def _apply_approval(story, pull_request, decision, comments, root):
    plan = approval_plan(story, pull_request, decision, comments)
    if not plan.ok:
        sys.stderr.write("REFUSED: arbitrate-review\n")
        for error in plan.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    run = ralph_review_render.run_plan(plan.commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: arbitrate-review (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        return 1
    print("OK: @%s approved #%s; the model-review gate is released on %s"
          % (decision.author, story["number"], plan.head))
    return 0


def _apply_requested_changes(story, pull_request, decision, config, root,
                             comments):
    """Hand the human's feedback to the Story's assigned implementation model.

    The label moves first: the Story is being worked again from that moment,
    and a crash mid-way should leave it In Review rather than stranded in
    `state:blocked` with work on the branch.  The arbitration is recorded
    *last*, so a launch that never happened is retried by the next tick rather
    than being marked as answered.
    """
    reopen = reopen_plan(story, decision)
    run = ralph_review_render.run_plan(reopen.commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: arbitrate-review (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        return 1

    head = pull_request.get("headRefOid")
    try:
        context, _diff = ralph_review_context.bundle_for(
            story, pull_request, ralph_review_round.next_round(pull_request),
            root, comments=comments, for_role="implementation")
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not assemble review context: %s\n" % exc)
        return 2
    if not context.ok:
        sys.stderr.write("REFUSED: arbitrate-review\n")
        for error in context.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    outcome, errors = ralph_agent.launch_role(
        config, "implementation", arbitration_prompt(decision, context.text),
        story=story)
    if outcome is None:
        sys.stderr.write("REFUSED: arbitrate-review\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    if outcome.kind != ralph_agent.NORMAL:
        sys.stderr.write("FAILED: arbitrate-review (implementation agent %s)\n"
                         % outcome.kind)
        return ralph_review_respond.EXIT_CODES.get(outcome.kind, 2)

    checkout = ralph_review_respond.Checkout(root)
    try:
        new_head = checkout.head()
    except RuntimeError as exc:
        sys.stderr.write("ralph: could not read the checkout's head: %s\n" % exc)
        return 2
    if new_head != head and not checkout.is_ancestor(head, new_head):
        # The same invariant every fix round holds to: the reviewed commit must
        # stay in the branch, or the threads and checks citing it are stranded.
        sys.stderr.write(
            "REFUSED: arbitrate-review\n  - head: %s is not a descendant of "
            "%s; work must be appended, never amended or force-pushed\n"
            % (new_head, head))
        return 2

    commands = []
    if new_head != head:
        commands.append(["git", "push", "origin", "HEAD"])
    commands.append(arbitration_record_command(story, pull_request, decision))
    run = ralph_review_render.run_plan(commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: arbitrate-review (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        return 1
    print("OK: @%s requested changes on #%s; the implementation model answered "
          "and %s is now %s" % (decision.author, story["number"], head, new_head))
    return 0


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_arbitrate(rest):
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
            "usage: ralph --arbitrate-review STORY [CONFIG] [ROOT] [--pr PATH]\n")
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
        sys.stderr.write("ralph: could not read the arbitration's inputs: %s\n"
                         % exc)
        return 2
    if pull_request is None:
        sys.stderr.write("REFUSED: arbitrate-review\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    return arbitrate(story, pull_request, validated.config, root)


def main(argv):
    if argv and argv[0] == "arbitrate":
        return _cmd_arbitrate(argv[1:])
    sys.stderr.write("usage: ralph_review_human.py arbitrate STORY [CONFIG] "
                     "[ROOT] [--pr PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
