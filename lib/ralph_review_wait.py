"""Bounded in-tick waiting for model review (#54).

The orchestration process owns waiting, not either model: the tick holds its
lock, polls durable GitHub state with backoff, and launches an agent only when
there is work to do.  Idle GitHub time therefore costs no tokens at all.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_complete  # noqa: E402
import ralph_review_deadlock  # noqa: E402
import ralph_review_human  # noqa: E402
import ralph_review_respond  # noqa: E402
import ralph_review_round  # noqa: E402

# What the tick may do about this pull request right now.
REVIEW = "review"      # the head has no review yet: launch a Negotiation Round
RESPOND = "respond"    # the review requested changes: answer it with a fix round
ESCALATE = "escalate"  # the rounds are spent and it is still unsettled: ask a human
ARBITRATE = "arbitrate"  # a human decided; act on it before anything else
COMPLETE = "complete"  # both halves of the gate are satisfied: finish the Story
HOLD = "hold"          # ready but for a protected control-plane path: ask a human
WAIT = "wait"          # nothing Ralph can act on; keep polling, spend nothing
GONE = "gone"          # no marked pull request, or it is no longer open

# How the wait itself ended (REVIEW is never an ending; it is a step).
EXPIRED = "expired"    # the bounded window closed: Handoff and end the tick
FAILED = "failed"      # a step did not complete; leave it for the next tick

# The tick reads the ending as an exit code. Window expiry is its own code
# because it is the one ending that obliges the caller to write a Handoff --
# and it is not a failure: the Story is intact and the next tick resumes it.
EXIT_WINDOW_EXPIRED = 14

# Deadlock is its own ending too, and also not a failure: one Story stopped,
# the loop did not. The caller may go straight on to the next Story rather than
# writing a Handoff for one that is now blocked.
EXIT_ESCALATED = 15

# And so is completion: the Story is finished, so the tick is free to go
# straight on to the next one rather than ending on it.
EXIT_COMPLETED = 16

# However long the window is, a poll never falls further apart than this: the
# backoff is there to stop hammering the API, not to sleep through the arrival
# of the thing being waited for.
MAX_POLL_SECONDS = 300


# A step can fail because the negotiation went wrong, or because the provider
# did.  Only the first is this tick's answer: a provider outage, a quota or
# authentication refusal, and output the wrapper would not publish all leave the
# pull request exactly as it was -- no review posted, so no Negotiation Round
# spent -- and the next poll may well find the outage over.  So they are retried
# inside the window rather than ending it (#61).
RETRYABLE_EXITS = (ralph_agent.EXIT_INFRASTRUCTURE_FAILURE,
                   ralph_review_round.EXIT_INVALID_OUTPUT)


def step_outcome(rc, what):
    """The ``(ok, errors, retryable)`` verdict on one step's exit code."""
    if rc == 0:
        return True, [], False
    return False, ["%s exited %d" % (what, rc)], rc in RETRYABLE_EXITS


class WaitResult:
    """How the wait ended, and what it cost."""

    def __init__(self, kind, errors=None, polls=0, invocations=0, elapsed=0,
                 retries=0):
        self.kind = kind
        self.errors = errors or []
        self.polls = polls
        self.invocations = invocations
        self.elapsed = elapsed
        # Provider failures ridden out inside the window. They cost invocations
        # but no Negotiation Round, so the two are counted separately.
        self.retries = retries


def next_step(pull_request, comments=None, max_rounds=None, protected=()):
    """The one decision a poll makes, from durable state only.

    `comments` are the Story's, where Ralph records each round's review result
    and each response to one.  `max_rounds` bounds the negotiation: once the
    budget is spent and the two models still owe each other a move, the
    disagreement is a human's to settle, not a third model's.  Anything Ralph
    cannot act on is ``WAIT``, which costs nothing at all -- no context, no
    invocation.
    """
    if not ralph_review.is_managed_pr(pull_request):
        return GONE
    if (pull_request.get("state") or "OPEN").upper() != "OPEN":
        return GONE
    head = pull_request.get("headRefOid")
    # A human's decision outranks everything the two models owe each other, so
    # it is read first -- and it is read from GitHub's own review controls,
    # which is the whole point: there are no commands to learn.
    decision = ralph_review_human.human_decision(pull_request)
    if decision is not None and not ralph_review.arbitrated(comments, decision.id):
        return ARBITRATE
    gate = ralph_review_complete.gate_for(pull_request, comments,
                                          protected=protected)
    if gate.ok:
        # Both halves hold at this exact commit: CI is green and the review
        # context is satisfied -- by an approving review, or by a human's
        # Approve, which open model findings never reopen.
        return COMPLETE
    if gate.held_for_human:
        # Everything a model can settle is settled, and the change touches the
        # control plane, so the last word is a person's. The notice is posted
        # once per head; after that there is simply nothing to do but wait.
        return WAIT if ralph_review.control_plane_held(comments, head) else HOLD
    # Reviews and answers alternate, so which side owes a move is simply which
    # of them is behind.  A dispute leaves the head where it was, which is why
    # this counts rounds at the head rather than asking whether the head has
    # ever been reviewed at all.
    owed = None
    if ralph_review.needs_review(pull_request, comments):
        owed = REVIEW
    elif (ralph_review.latest_result(comments, head) or {}).get("verdict") \
            == "request_changes":
        owed = RESPOND
    if owed is None:
        return WAIT
    # The budget is spent in *rounds*, counted against this **Story**: a review
    # of an amended head and a re-review after a dispute both cost one, because
    # both spend an invocation on the same disagreement.  Counted on the Story
    # rather than on the pull request, so a Story's budget is its own however
    # many Stories preceded it in the same Feature.
    spent = ralph_review.rounds_spent(comments)
    if max_rounds is not None and spent >= max_rounds:
        return ESCALATE
    return owed


class WaitPolicy:
    """How long to wait for review, how often to look, and for how many rounds."""

    def __init__(self, window_seconds, first_poll, max_poll=MAX_POLL_SECONDS,
                 max_rounds=None):
        self.window_seconds = window_seconds
        self.first_poll = first_poll
        self.max_poll = max_poll
        self.max_rounds = max_rounds

    @classmethod
    def from_config(cls, config):
        """The target repository's window, in the units it configures it in."""
        review = (config or {}).get("review") or {}
        return cls(window_seconds=review["wait_minutes"] * 60,
                   first_poll=review["poll_seconds"],
                   max_rounds=review["max_rounds"])

    def expired(self, elapsed):
        return elapsed >= self.window_seconds

    def sleep_for(self, poll_index, elapsed):
        """How long to sleep before poll number *poll_index* (0-based).

        The window is the bound, so the final sleep is clipped to what is left
        of it rather than overrunning it -- a wait that ends late would hold the
        tick's lock past the point where a Handoff was due.
        """
        remaining = self.window_seconds - elapsed
        if remaining <= 0:
            return 0
        return min(self.first_poll * (2 ** poll_index), self.max_poll, remaining)


def await_review(policy, fetch, act, sleep, now, read_comments=None,
                 read_protected=None):
    """Poll durable state until the negotiation moves on or the window closes.

    ``fetch`` reads the pull request and ``read_comments`` the Story's recorded
    rounds; ``act`` runs the step those imply and returns ``(ok, errors)``.
    Everything between polls is sleep: waiting spends no context and makes no
    model invocation, which is the point of doing it here rather than inside an
    agent.  One window can carry a whole exchange -- review, answer, review of
    the answered head -- because each step changes the durable state the next
    poll reads.
    """
    read_comments = read_comments or (lambda: [])
    read_protected = read_protected or (lambda pull_request: ())
    started, polls, invocations, retries = now(), 0, 0, 0
    last_errors = []
    while True:
        pull_request = fetch()
        polls += 1
        step = next_step(pull_request, read_comments(),
                         max_rounds=policy.max_rounds,
                         protected=read_protected(pull_request))
        elapsed = now() - started
        if step == GONE:
            return WaitResult(GONE, polls=polls, invocations=invocations,
                              elapsed=elapsed, retries=retries)
        if step in (REVIEW, RESPOND, ESCALATE, ARBITRATE, COMPLETE, HOLD):
            # Escalation is Ralph's own bookkeeping -- a comment, a review
            # request, a label -- so it costs no model invocation. Arbitration
            # may or may not launch one, depending on what the human decided.
            if step not in (ESCALATE, ARBITRATE, COMPLETE, HOLD):
                invocations += 1
            ok, errors, retryable = act(step, pull_request)
            if not ok and not retryable:
                # Retrying a step that failed on its own terms every poll would
                # spend an invocation each time; the next tick retries it once.
                return WaitResult(FAILED, errors=errors, polls=polls,
                                  invocations=invocations, elapsed=elapsed,
                                  retries=retries)
            if not ok:
                # The provider failed, not the negotiation: the pull request is
                # untouched, so this is simply a poll that achieved nothing.
                # The backoff between polls is what keeps it from being a storm.
                retries += 1
                last_errors = errors
            elif step in (ESCALATE, COMPLETE):
                # Nothing is left to wait for. Either the Story is blocked with
                # a human asked, or it is finished. Sitting out the rest of the
                # window would hold the tick's lock over work that has stopped.
                return WaitResult(step, polls=polls, invocations=invocations,
                                  elapsed=now() - started, retries=retries)
            elapsed = now() - started
        if policy.expired(elapsed):
            return WaitResult(EXPIRED, errors=last_errors, polls=polls,
                              invocations=invocations, elapsed=elapsed,
                              retries=retries)
        sleep(policy.sleep_for(polls - 1, elapsed))


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_await(rest):
    if not 1 <= len(rest) <= 3 or not rest[0]:
        sys.stderr.write("usage: ralph --await-review STORY [CONFIG] [ROOT]\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"
    root = os.path.abspath(rest[2] if len(rest) > 2 and rest[2] else os.getcwd())

    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for error in validated.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    try:
        story = _load(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    seen = {"pull_request": None}

    def fetch():
        # Durable state only: whatever this reads, another tick or a human
        # would read the same. Once a pull request is known, a gh blip reads as
        # "nothing new" rather than "it is gone", so an outage cannot end the
        # window early or, worse, look like a resolved negotiation.
        try:
            pull_request, _ = ralph_review_round.discover_pull_request(
                story, cwd=root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read review state: %s\n" % exc)
            return seen["pull_request"]
        seen["pull_request"] = pull_request
        return pull_request

    def read_comments():
        # The Story is where each round's review result and each response are
        # recorded, so this is what tells a poll whether a review is answered.
        try:
            return ralph_review_respond.story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return []

    def act(step, pull_request):
        if step == HOLD:
            try:
                protected = ralph_review_complete.protected_for(
                    pull_request, validated.config, root)
            except (OSError, ValueError, RuntimeError) as exc:
                return False, ["could not read the reviewed diff: %s" % exc], False
            rc = ralph_review_complete.hold(story, pull_request,
                                            validated.config, root, protected)
            return step_outcome(rc, "control-plane hold")
        if step == COMPLETE:
            rc = ralph_review_complete.complete(story, pull_request,
                                                validated.config, root)
            return step_outcome(rc, "completion")
        if step == ARBITRATE:
            rc = ralph_review_human.arbitrate(story, pull_request,
                                              validated.config, root)
            return step_outcome(rc, "arbitration")
        if step == ESCALATE:
            rc = ralph_review_deadlock.escalate(story, pull_request,
                                                validated.config, root)
            return step_outcome(rc, "escalation")
        if step == RESPOND:
            rc = ralph_review_respond.respond_to_review(
                story, pull_request, validated.config, root)
            return step_outcome(rc, "response round")
        rc = ralph_review_round.run_round(story, pull_request,
                                          validated.config, root)
        return step_outcome(rc, "review round")

    def read_protected(pull_request):
        # The control-plane policy is read from the diff of whatever head the
        # poll just saw: a fix pushed mid-window can bring a protected path
        # into the change, and the rule has to notice.
        if pull_request is None:
            return ()
        try:
            return ralph_review_complete.protected_for(
                pull_request, validated.config, root)
        except (OSError, ValueError, RuntimeError) as exc:
            # Failing open would let a control-plane change merge on a model
            # review alone, which is the one thing this rule exists to stop.
            sys.stderr.write("ralph: could not read the reviewed diff: %s\n" % exc)
            return ("(unknown -- the reviewed diff could not be read)",)

    policy = WaitPolicy.from_config(validated.config)
    result = await_review(policy, fetch=fetch, act=act, sleep=time.sleep,
                          now=time.monotonic, read_comments=read_comments,
                          read_protected=read_protected)
    number = story.get("number", "?")
    if result.kind == EXPIRED:
        print("OK: review window closed after %.0fs on #%s (%d poll%s, %d "
              "invocation%s); a Handoff is due"
              % (result.elapsed, number, result.polls,
                 "" if result.polls == 1 else "s", result.invocations,
                 "" if result.invocations == 1 else "s"))
        if result.retries:
            # Named, because a window spent entirely on outages looks exactly
            # like a quiet one in the log otherwise -- and the two call for
            # very different operator attention.
            print("note: %d invocation%s failed on infrastructure and consumed "
                  "no Negotiation Round; the next tick resumes the same round"
                  % (result.retries, "" if result.retries == 1 else "s"))
            for error in result.errors:
                print("  - %s" % error)
        return EXIT_WINDOW_EXPIRED
    if result.kind == ESCALATE:
        print("OK: #%s is blocked after a deadlocked negotiation; a human was "
              "asked to arbitrate. Unrelated Stories keep running." % number)
        return EXIT_ESCALATED
    if result.kind == HOLD:
        print("OK: #%s touches the protected control plane; a human was asked "
              "to approve before it can complete" % number)
        return 0
    if result.kind == COMPLETE:
        print("OK: #%s passed both halves of the gate and was completed" % number)
        return EXIT_COMPLETED
    if result.kind == GONE:
        print("OK: nothing to negotiate for #%s; the pull request is not open "
              "for automated review" % number)
        return 0
    sys.stderr.write("FAILED: await-review (%s)\n" % result.kind)
    for error in result.errors:
        sys.stderr.write("  - %s\n" % error)
    return 1


def main(argv):
    if argv and argv[0] == "await":
        return _cmd_await(argv[1:])
    sys.stderr.write("usage: ralph_review_wait.py await STORY [CONFIG] [ROOT]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
