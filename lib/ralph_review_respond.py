"""Answer one round of review findings with an append-only fix round (#55).

The Implementation Agent is the only role permitted to edit, commit and push,
so it -- not Ralph -- writes the fixes.  What Ralph owns is the boundary around
that: which findings are open, whether the answer is well-formed, whether the
new head really was appended rather than rewritten, and how the answer reaches
the pull request.
"""
import json
import os
import subprocess
import sys

try:
    import jsonschema
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write("ralph: jsonschema is required (pip install jsonschema)\n")
    raise

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_round  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESPOND_PROMPT = os.path.join(REPO_ROOT, "prompts", "respond.v1.md")
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "schema", "response.schema.json")

CONTRACT_VERSION = "ralph-response/v1"

# How the round ended.
RESPONDED = "responded"
INVALID_OUTPUT = "invalid-output"
NOT_APPEND_ONLY = "not-append-only"
REFUSED = "refused"

# The tick reads the outcome as an exit code, the same contract the review
# round uses: a provider that never finished is distinguishable from an answer
# that was refused.
EXIT_CODES = {
    INVALID_OUTPUT: 2,
    NOT_APPEND_ONLY: 2,
    REFUSED: 2,
    ralph_agent.SESSION_EXHAUSTED: ralph_agent.EXIT_SESSION_EXHAUSTED,
    ralph_agent.INFRASTRUCTURE_FAILURE: ralph_agent.EXIT_INFRASTRUCTURE_FAILURE,
}

ACCEPTED = "accepted"
DISPUTED = "disputed"
UNRESOLVED = "unresolved"
DISPOSITIONS = (ACCEPTED, DISPUTED, UNRESOLVED)


class RespondResult:
    def __init__(self, ok, errors, kind, head=None, new_head=None,
                 response=None):
        self.ok = ok
        self.errors = errors
        self.kind = kind
        self.head = head
        self.new_head = new_head
        self.response = response


def open_findings(result):
    """The findings this round must answer."""
    return list((result or {}).get("findings") or [])


def respond_prompt(result, context, prompt_path=RESPOND_PROMPT):
    """The instructions, the findings to answer, then the evidence.

    The findings are handed over as the contract JSON the reviewer emitted, not
    as the Markdown they were rendered into: the agent answers by identifier,
    and identifiers survive verbatim.
    """
    with open(prompt_path) as fh:
        instructions = fh.read().rstrip()
    return "%s\n\n---\n\n## Open findings (round %s, commit %s)\n\n```json\n%s\n```\n\n---\n\n%s" % (
        instructions, result.get("round"), result.get("head"),
        json.dumps({"findings": open_findings(result)}, indent=2), context)


def validate_response(payload, result, schema_path=DEFAULT_SCHEMA):
    """Validate one response against the contract and the round it answers.

    Two things must hold, and neither is checkable from the payload alone: the
    response answers *this* round's exact commit, and it accounts for every
    finding that round raised -- exactly once, and none it never raised. A
    partial answer would leave the loop unable to say whether a blocker was
    handled, which is the one thing the record exists to state.
    """
    if not isinstance(payload, dict):
        return ["(root): response payload must be an object"]
    with open(schema_path) as fh:
        schema = json.load(fh)
    errors = sorted(jsonschema.Draft7Validator(schema).iter_errors(payload),
                    key=lambda e: list(e.absolute_path))
    if errors:
        return [ralph_config.format_error(error) for error in errors]

    problems = []
    if payload["head"] != result.get("head"):
        problems.append(
            "head: the response answers %s but the review judged %s"
            % (payload["head"], result.get("head")))

    open_ids = [f.get("id") for f in open_findings(result)]
    answered = [d["id"] for d in payload["dispositions"]]
    for index, ident in enumerate(answered):
        if ident not in open_ids:
            problems.append("dispositions/%d/id: %r was not raised by round %s"
                            % (index, ident, result.get("round")))
        elif answered.count(ident) > 1:
            problems.append("dispositions/%d/id: %r is answered more than once"
                            % (index, ident))
    for ident in open_ids:
        if ident not in answered:
            problems.append("dispositions: finding %r has no disposition" % ident)
    return problems


def accepted(response):
    """The dispositions claiming a fix was made."""
    return [d for d in (response or {}).get("dispositions") or []
            if d.get("disposition") == ACCEPTED]


def append_only_errors(response, reviewed_head, new_head, checkout):
    """Check the branch grew from the reviewed commit rather than replacing it.

    This is the invariant, verified rather than trusted: the reviewed commit
    must still be reachable from the new head. An amend, a rebase or a
    force-push all fail exactly this test, and each of them would strand the
    review threads, the checks and the commit evidence that cite it.

    The head and the dispositions must also agree in both directions. An
    accepted finding with an unchanged head is a fix nobody made; a moved head
    with nothing accepted is a change nobody asked for -- a dispute argues from
    evidence, it does not quietly edit the code it is arguing about.
    """
    if new_head == reviewed_head:
        if accepted(response):
            return ["head: %d finding(s) were accepted but the head is still "
                    "%s; an accepted finding needs a fix commit behind it"
                    % (len(accepted(response)), reviewed_head)]
        return []
    if not accepted(response):
        return ["head: the head moved to %s but no finding was accepted; a "
                "dispute or an unresolved finding changes no code" % new_head]
    if not checkout.is_ancestor(reviewed_head, new_head):
        return ["head: %s is not a descendant of the reviewed commit %s; fixes "
                "must be appended, never amended or force-pushed"
                % (new_head, reviewed_head)]
    return []


def _thread_for(finding_id, review_comments):
    """The inline thread carrying *finding_id*, if the finding had a location.

    Threads are matched on the rendered heading Ralph itself wrote (`**F-3**`),
    so a finding id appearing inside someone's prose cannot claim the thread.
    """
    heading = "**%s**" % finding_id
    for comment in review_comments or []:
        if ((comment or {}).get("body") or "").lstrip().startswith(heading):
            return comment
    return None


def disposition_body(disposition):
    """One answered finding as Markdown, wherever the answer is written.

    A dispute's evidence travels with it. The reply and the consolidated
    comment are the only places a human -- or the next fresh reviewer -- meets
    the argument, so an evidence-free dispute must not be able to look like a
    reasoned one anywhere.
    """
    lines = ["**%s** — %s" % (disposition["id"], disposition["disposition"]),
             "", disposition["note"]]
    if disposition.get("evidence"):
        lines += ["", "- _Evidence_: %s" % disposition["evidence"]]
    return "\n".join(lines)


def reply_commands(response, review_comments, pull_number):
    """One reply per answered finding that has a thread to answer in.

    A cross-cutting finding was rendered in the review body and has no thread;
    it is answered by the consolidated record instead of being forced into an
    inline reply it does not belong in.
    """
    commands = []
    for disposition in (response or {}).get("dispositions") or []:
        thread = _thread_for(disposition["id"], review_comments)
        if thread is None:
            continue
        commands.append([
            "gh", "api", "--method", "POST",
            "repos/{owner}/{repo}/pulls/%s/comments/%s/replies"
            % (pull_number, thread["id"]),
            "-f", "body=" + disposition_body(disposition),
        ])
    return commands


def response_comment(response, new_head):
    """The consolidated answer: prose for people, the record for the loop."""
    lines = [
        "Ralph implementation response — round %s" % response["round"],
        "",
        "Implementing model: `%s`" % response["model"],
        "Reviewed commit: %s" % response["head"],
        "New head: %s" % new_head,
        "",
        response["summary"],
        "",
    ]
    for disposition in response["dispositions"]:
        lines += [disposition_body(disposition), ""]
    lines += ["", ralph_review.response_record(response)]
    return "\n".join(lines)


def conduct(story, pull_request, result, context, launch, publish, checkout):
    """Run one response round; return a RespondResult."""
    head = pull_request.get("headRefOid")
    outcome, errors = launch(respond_prompt(result, context))
    if outcome is None:
        return RespondResult(False, errors, REFUSED, head=head)
    if outcome.kind != ralph_agent.NORMAL:
        return RespondResult(False, ["implementation agent %s (exit %s)"
                                     % (outcome.kind, outcome.exit_code)],
                             outcome.kind, head=head)
    answer = ralph_review_round.extract_result(outcome.output)
    errors = validate_response(answer, result)
    if errors:
        return RespondResult(False, errors, INVALID_OUTPUT, head=head)
    new_head = checkout.head()
    errors = append_only_errors(answer, head, new_head, checkout)
    if errors:
        return RespondResult(False, errors, NOT_APPEND_ONLY, head=head,
                             new_head=new_head, response=answer)
    ok, errors = publish(answer, new_head)
    return RespondResult(ok, errors, RESPONDED, head=head, new_head=new_head,
                         response=answer)


class Checkout:
    """The two git facts a response round needs, read from a real checkout."""

    def __init__(self, root):
        self.root = root

    def _git(self, args):
        return subprocess.run(["git"] + args, cwd=self.root,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)

    def head(self):
        proc = self._git(["rev-parse", "HEAD"])
        if proc.returncode:
            raise RuntimeError(proc.stdout.strip() or "git rev-parse failed")
        return proc.stdout.strip()

    def is_ancestor(self, old, new):
        return self._git(["merge-base", "--is-ancestor", old, new]).returncode == 0


def _gh(args, cwd):
    proc = subprocess.run(["gh"] + list(args), cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "gh command failed")
    return proc.stdout


# The negotiation's durable state is read in one place (#53), and every stage
# that needs it reads it there rather than growing its own gh spelling.
story_comments = ralph_review_round.story_comments


def review_threads(pull_number, cwd):
    """The pull request's inline review comments, with their thread ids."""
    return json.loads(_gh(
        ["api", "repos/{owner}/{repo}/pulls/%s/comments" % pull_number],
        cwd) or "[]")


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_respond(rest):
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
            "usage: ralph --respond-review STORY [CONFIG] [ROOT] [--pr PATH]\n")
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
            pull_request, errors = ralph_review_round.discover_pull_request(
                story, cwd=root)
        if pull_request is not None:
            comments = story_comments(story["number"], root)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not read the round's inputs: %s\n" % exc)
        return 2
    if pull_request is None:
        sys.stderr.write("REFUSED: respond-review\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    return respond_to_review(story, pull_request, validated.config, root,
                             comments=comments)


def respond_to_review(story, pull_request, config, root, comments=None):
    """Answer the recorded review of this head; return an exit code.

    Shared by `--respond-review` and the in-tick wait (#54), so a round started
    by a poll and one started by hand take exactly the same path.
    """
    head = pull_request.get("headRefOid")
    if comments is None:
        try:
            comments = story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return 2
    result = ralph_review.latest_result(comments, head)
    if result is None:
        sys.stderr.write("REFUSED: respond-review\n  - review: no recorded "
                         "review of %s to answer\n" % head)
        return 2
    if ralph_review.needs_review(pull_request, comments):
        # Every round this head has had is already answered. What it is owed is
        # a fresh reviewer's adjudication, not a second answer to a settled
        # question -- and answering anyway would spend an invocation on one.
        sys.stderr.write("REFUSED: respond-review\n  - review: round %s of %s "
                         "is already answered; the next move is a fresh "
                         "review\n" % (result.get("round"), head))
        return 2
    if result.get("verdict") != "request_changes":
        sys.stderr.write("REFUSED: respond-review\n  - verdict: round %s "
                         "returned %r; there is nothing to answer\n"
                         % (result.get("round"), result.get("verdict")))
        return 2

    try:
        context, _diff = ralph_review_context.bundle_for(
            story, pull_request, result.get("round", 1), root,
            comments=comments, for_role="implementation")
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not assemble review context: %s\n" % exc)
        return 2
    if not context.ok:
        sys.stderr.write("REFUSED: respond-review\n")
        for error in context.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2

    def launch(prompt):
        return ralph_agent.launch_role(config, "implementation", prompt,
                                       story=story)

    def publish(answer, new_head):
        # The push comes first: until the fix is on the remote there is no new
        # head for CI and the next review round to judge, and the replies would
        # describe commits nobody else can see. Never --force -- the invariant
        # this round is built on is that the branch only grows.
        number = pull_request["number"]
        pushed = ralph_review_render.run_plan([["git", "push", "origin", "HEAD"]],
                                              cwd=root)
        if not pushed.ok:
            return False, ["git push failed: %s"
                           % (pushed.failed.output.strip() or "unknown error")]
        # Replies next, then the consolidated record: the record is the loop's
        # signal that this head is answered, so it is written only once the
        # answer is actually on the pull request.
        try:
            threads = review_threads(number, root)
        except (ValueError, RuntimeError) as exc:
            return False, ["could not read the review threads: %s" % exc]
        commands = reply_commands(answer, threads, number)
        commands.append(["gh", "pr", "comment", str(number), "--body",
                         response_comment(answer, new_head)])
        run = ralph_review_render.run_plan(commands, cwd=root)
        if not run.ok:
            return False, ["%s: exit %d" % (" ".join(run.failed.args),
                                            run.failed.returncode)]
        return True, []

    outcome = conduct(story, pull_request, result, context.text, launch,
                      publish, Checkout(root))
    if outcome.ok:
        print("OK: answered round %s of #%s; %s is now %s"
              % (result.get("round"), story["number"], outcome.head,
                 outcome.new_head))
        return 0
    sys.stderr.write("REFUSED: respond-review (%s)\n" % outcome.kind)
    for error in outcome.errors:
        sys.stderr.write("  - %s\n" % error)
    return EXIT_CODES.get(outcome.kind, 2)


def main(argv):
    if argv and argv[0] == "respond":
        return _cmd_respond(argv[1:])
    sys.stderr.write("usage: ralph_review_respond.py respond STORY [CONFIG] "
                     "[ROOT] [--pr PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
