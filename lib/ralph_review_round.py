"""One Negotiation Round against an exact pull-request head (#53).

The round is the seam that spends a model invocation, so it owns the two
questions nothing else can answer: is this head already reviewed, and is what
came back publishable?  Launching and publishing are injected, keeping this
module pure and the ordering testable.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_ledger  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_result  # noqa: E402
import ralph_usage  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW_PROMPT = os.path.join(REPO_ROOT, "prompts", "review.v1.md")

# What one round did.
PUBLISHED = "published"
ALREADY_REVIEWED = "already-reviewed"
INVALID_OUTPUT = "invalid-output"
REFUSED = "refused"

# Output the wrapper would not publish is the provider's failure, not the
# negotiation's: nothing was posted, so the head is still unjudged and no
# Negotiation Round was spent.  It therefore needs a code of its own, distinct
# from the flat refusal (2) that a bad config or an unmarked pull request
# returns -- those would refuse identically on the next attempt, and this one
# may not (#61).
EXIT_INVALID_OUTPUT = 17

# The tick reads a round's outcome as an exit code.  A provider outcome keeps
# the code `--launch-agent` already uses for it, so the caller distinguishes a
# reviewer that never finished from a review that was refused.
EXIT_CODES = {
    PUBLISHED: 1,               # validated and rendered, but gh refused it
    INVALID_OUTPUT: EXIT_INVALID_OUTPUT,
    REFUSED: 2,
    ralph_agent.SESSION_EXHAUSTED: ralph_agent.EXIT_SESSION_EXHAUSTED,
    ralph_agent.INFRASTRUCTURE_FAILURE: ralph_agent.EXIT_INFRASTRUCTURE_FAILURE,
}


class RoundResult:
    def __init__(self, ok, errors, kind, round_no=None, head=None,
                 invocations=0, review=None, usage_event=None):
        self.ok = ok
        self.errors = errors
        self.kind = kind
        self.round_no = round_no
        self.head = head
        self.invocations = invocations
        self.review = review
        # What this round's invocation cost (#62), or None when it made none.
        # Carried out rather than written here, so `conduct` stays pure.
        self.usage_event = usage_event


def extract_result(output):
    """The structured result a Review Agent emitted, or None.

    A provider CLI prints prose around the answer, so the contract object is
    recovered from the output rather than assumed to be all of it.  Extraction
    never repairs anything: whatever comes back still faces the #51 validator
    whole, so a partial or invented object is refused there, not smuggled in.
    """
    text = output or ""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    for candidate in reversed(list(_json_objects(text))):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _json_objects(text):
    """Yield every balanced top-level ``{...}`` span in *text*, in order."""
    depth, start, in_string, escaped = 0, None, False, False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                yield text[start:index + 1]


def next_round(pull_request):
    """Which Negotiation Round this pull request is owed next.

    Counted from the reviews Ralph has already published, so the number is a
    fact about the pull request rather than loop-local bookkeeping.  Reviews,
    not distinct heads: a disputed round is judged again at the same commit,
    and counting heads would hand it the number it just used -- which would
    make a round limit unreachable by a model that only ever disputes.  A human
    review is not a round and never advances it.
    """
    return len(ralph_review.review_stamps(pull_request)) + 1


def review_prompt(context, prompt_path=REVIEW_PROMPT):
    """The instructions, then the evidence they govern.

    The judgement-heavy half of a round is checked in and drift-guarded, the
    same way the iteration and handoff prompts are; only the per-round evidence
    is assembled at runtime.
    """
    with open(prompt_path) as fh:
        return "%s\n\n---\n\n%s" % (fh.read().rstrip(), context)


def conduct(story, pull_request, context, launch, publish, changed=None,
            comments=None):
    """Run one round: launch the reviewer, validate, then publish."""
    head = pull_request.get("headRefOid")
    # The guard comes first, so a head that already has its answer costs
    # nothing: a round is a model invocation, and a second one over the same
    # unanswered judgement buys nothing.  `comments` are the Story's, where
    # each response is recorded -- an answered head is owed a fresh round even
    # though the commit has not moved, because that is what a dispute is.
    if not ralph_review.needs_review(pull_request, comments):
        return RoundResult(True, [], ALREADY_REVIEWED, head=head)
    round_no = next_round(pull_request)
    outcome, errors = launch(review_prompt(context))
    if outcome is None:
        return RoundResult(False, errors, REFUSED, head=head)
    # The invocation happened, so it is accounted for -- whatever came back.
    # A ledger that counted only the rounds that published would understate
    # exactly the weeks worth understanding (#62).
    event = ralph_usage.invocation_event(
        outcome.usage, role="review", phase=ralph_usage.REVIEW,
        model=outcome.model, provider=outcome.provider,
        story=story.get("number"), pull_request=pull_request.get("number"),
        round_no=round_no, head=head)
    if outcome.kind != ralph_agent.NORMAL:
        # The reviewer never finished, so there is no judgement to reject.
        # Reporting the provider outcome verbatim keeps that distinction
        # available to the caller deciding whether a round was consumed.
        return RoundResult(False, ["review agent %s (exit %s)"
                                   % (outcome.kind, outcome.exit_code)],
                           outcome.kind, head=head, invocations=1,
                           usage_event=event)
    payload = extract_result(outcome.output)
    validation = ralph_review_result.validate_review(
        payload, changed=changed,
        prior_findings=ralph_review.previous_findings(comments))
    if not validation.ok:
        return RoundResult(False, validation.errors, INVALID_OUTPUT, head=head,
                           invocations=1, usage_event=event)
    if validation.review["head"] != head:
        # The published review stamps the commit it names, and that stamp is
        # what marks this head reviewed.  Accepting a result for another commit
        # would both publish findings about code nobody asked about and leave
        # this head unmarked, so every later tick would review it again.
        return RoundResult(
            False, ["head: the reviewer judged %s, not the pull request head %s"
                    % (validation.review["head"], head)],
            INVALID_OUTPUT, head=head, invocations=1, usage_event=event)
    # The review carries its own invocation's cost as a footer (#63), so
    # publishing has to know it -- hence the second argument.
    ok, errors = publish(validation.review, event)
    return RoundResult(ok, errors, PUBLISHED, round_no=validation.review["round"],
                       head=head, invocations=1, review=validation.review,
                       usage_event=event)


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


# The fields a round needs about a pull request: the exact commits it is bound
# to, the CI snapshot it reports without waiting for, and the durable
# discussion that is the only prior-round evidence it may read.
PR_FIELDS = ("number,body,baseRefOid,headRefOid,statusCheckRollup,"
             "comments,reviews")


def _gh(args, cwd):
    proc = subprocess.run(["gh"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "gh command failed")
    return proc.stdout


def review_threads(pull_number, cwd):
    """The pull request's inline review comments, with their thread ids.

    Not available through `gh pr view --json`, which reports reviews but not
    the replies hanging off them -- and the replies are where a human answers
    a finding directly, which is evidence the next round must not miss.
    """
    return json.loads(_gh(
        ["api", "repos/{owner}/{repo}/pulls/%s/comments" % pull_number],
        cwd) or "[]")


def story_comments(number, cwd):
    """The Story's comments, where each round's review and answer is recorded.

    The negotiation's state lives here rather than on the pull request, so
    every stage that has to know whether the current head is reviewed, answered
    or settled reads the same one place.
    """
    data = json.loads(_gh(["issue", "view", str(number), "--json", "comments"],
                          cwd) or "{}")
    return data.get("comments") or []


def references_story(pull_request, number):
    """Does this pull request carry the Story reference #49 writes into it?"""
    return re.search(r"Refs #%d\b" % number,
                     (pull_request or {}).get("body") or "") is not None


def discover_pull_request(story, cwd=None):
    """Read the story's open, Ralph-managed pull request from gh.

    Discovery is by the durable marker plus the ``Refs #N`` line the promotion
    stage writes, never by branch name or title: a human pull request may share
    either, and only the marker is the automated-review opt-in.
    """
    prs = json.loads(_gh(["pr", "list", "--state", "open",
                          "--json", "number,body"], cwd) or "[]")
    managed = [pr for pr in ralph_review.review_candidates(prs)
               if references_story(pr, story["number"])]
    if not managed:
        return None, ["pull_request: no Ralph-managed pull request references #%s"
                      % story["number"]]
    if len(managed) > 1:
        return None, ["pull_request: #%s has %d open Ralph-managed pull requests (%s)"
                      % (story["number"], len(managed),
                         ", ".join("#%s" % pr["number"] for pr in managed))]
    number = managed[0]["number"]
    pull_request = json.loads(_gh(["pr", "view", str(number), "--json",
                                   PR_FIELDS], cwd))
    # Attached rather than fetched again downstream: the bundle, the response
    # round's replies and any later audit all want the same read.
    pull_request["reviewThreads"] = review_threads(number, cwd)
    return pull_request, []


def _cmd_round(rest):
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
            "usage: ralph --review-round STORY [CONFIG] [ROOT] [--pr PATH]\n")
        return 2
    story_path = args[0]
    config_path = args[1] if len(args) > 1 and args[1] else ".ralph.yml"
    root = os.path.abspath(args[2] if len(args) > 2 and args[2] else os.getcwd())

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for error in validated.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    try:
        story = _load(story_path)
        if pr_path:
            pull_request, errors = _load(pr_path), []
        else:
            pull_request, errors = discover_pull_request(story, cwd=root)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not read the round's inputs: %s\n" % exc)
        return 2
    if pull_request is None:
        sys.stderr.write("REFUSED: review-round\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    # Refused before any evidence is assembled, so a pull request outside the
    # opt-in boundary costs zero model invocations. The already-reviewed guard
    # is `run_round`'s, and comes first there for the same reason.
    if not ralph_review.is_managed_pr(pull_request):
        sys.stderr.write(
            "REFUSED: review-round\n  - pull_request: #%s is not Ralph-managed\n"
            % pull_request.get("number", "?"))
        return 2
    return run_round(story, pull_request, validated.config, root)


def run_round(story, pull_request, config, root, comments=None):
    """Run one round against an already-read pull request; return an exit code.

    Shared by `--review-round` and the in-tick wait (#54), so a round started by
    a poll and one started by hand take exactly the same path.
    """
    head = pull_request.get("headRefOid")
    if comments is None:
        try:
            comments = story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return 2
    if not ralph_review.needs_review(pull_request, comments):
        print("OK: %s is already reviewed on PR #%s; no invocation spent"
              % (head, pull_request.get("number", "?")))
        return 0

    round_no = next_round(pull_request)
    try:
        context, diff = ralph_review_context.bundle_for(
            story, pull_request, round_no, root, comments=comments)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not assemble review context: %s\n" % exc)
        return 2
    if not context.ok:
        sys.stderr.write("REFUSED: review-round\n")
        for error in context.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    def launch(prompt):
        return ralph_agent.launch_role(config, "review", prompt, story=story)

    # What the round before this one raised, so a later round's review states
    # the fate of every open finding by identifier rather than leaving a
    # withdrawal to be inferred from silence.
    prior_findings = ralph_review.previous_findings(comments)

    def publish(review, usage_event):
        posted = ralph_review_render.publish(review, pull_request, cwd=root,
                                             story_number=story.get("number"),
                                             prior_findings=prior_findings,
                                             usage_event=usage_event)
        return posted.ok, posted.errors

    result = conduct(story, pull_request, context.text, launch, publish,
                     changed=ralph_review_result.changed_lines(diff),
                     comments=comments)
    # Emitted here rather than inside `conduct`, which stays pure. One line per
    # invocation, whatever the invocation produced (#62).
    ralph_usage.emit(result.usage_event)
    ralph_ledger.record(story.get("number"), result.usage_event, cwd=root,
                        comments=comments)
    number = pull_request.get("number", "?")
    if result.kind == ALREADY_REVIEWED:
        print("OK: %s is already reviewed on PR #%s; no invocation spent"
              % (result.head, number))
        return 0
    if result.ok:
        print("OK: round %s reviewed %s on PR #%s"
              % (result.round_no, result.head, number))
        return 0
    if result.kind == INVALID_OUTPUT:
        sys.stderr.write("REFUSED: review-round (the reviewer's result was not "
                         "publishable; nothing was posted)\n")
        for error in result.errors:
            sys.stderr.write("  - %s\n" % error)
        return EXIT_CODES[INVALID_OUTPUT]
    sys.stderr.write("FAILED: review-round (%s)\n" % result.kind)
    for error in result.errors:
        sys.stderr.write("  - %s\n" % error)
    return EXIT_CODES.get(result.kind, 2)


def main(argv):
    if argv and argv[0] == "round":
        return _cmd_round(argv[1:])
    sys.stderr.write("usage: ralph_review_round.py round STORY [CONFIG] [ROOT] "
                     "[--pr PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
