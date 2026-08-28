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
import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_round  # noqa: E402

# What the tick may do about this pull request right now.
REVIEW = "review"      # the head has no review yet: launch a Negotiation Round
WAIT = "wait"          # nothing Ralph can act on; keep polling, spend nothing
GONE = "gone"          # no marked pull request, or it is no longer open

# How the wait itself ended (REVIEW is never an ending; it is a step).
EXPIRED = "expired"    # the bounded window closed: Handoff and end the tick
FAILED = "failed"      # a step did not complete; leave it for the next tick

# The tick reads the ending as an exit code. Window expiry is its own code
# because it is the one ending that obliges the caller to write a Handoff --
# and it is not a failure: the Story is intact and the next tick resumes it.
EXIT_WINDOW_EXPIRED = 14

# However long the window is, a poll never falls further apart than this: the
# backoff is there to stop hammering the API, not to sleep through the arrival
# of the thing being waited for.
MAX_POLL_SECONDS = 300


class WaitResult:
    """How the wait ended, and what it cost."""

    def __init__(self, kind, errors=None, polls=0, invocations=0, elapsed=0):
        self.kind = kind
        self.errors = errors or []
        self.polls = polls
        self.invocations = invocations
        self.elapsed = elapsed


def next_step(pull_request):
    """The one decision a poll makes, from durable state only.

    Later stories widen this (responding to findings, human arbitration,
    completion); until then anything Ralph cannot act on is ``WAIT``, which
    costs nothing at all -- no context, no invocation.
    """
    if not ralph_review.is_managed_pr(pull_request):
        return GONE
    if (pull_request.get("state") or "OPEN").upper() != "OPEN":
        return GONE
    if ralph_review.is_reviewed(pull_request, pull_request.get("headRefOid")):
        return WAIT
    return REVIEW


class WaitPolicy:
    """How long to wait for review, and how often to look."""

    def __init__(self, window_seconds, first_poll, max_poll=MAX_POLL_SECONDS):
        self.window_seconds = window_seconds
        self.first_poll = first_poll
        self.max_poll = max_poll

    @classmethod
    def from_config(cls, config):
        """The target repository's window, in the units it configures it in."""
        review = (config or {}).get("review") or {}
        return cls(window_seconds=review["wait_minutes"] * 60,
                   first_poll=review["poll_seconds"])

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


def await_review(policy, fetch, act, sleep, now):
    """Poll durable state until the negotiation moves on or the window closes.

    ``fetch`` reads the pull request, ``act`` runs one Negotiation Round and
    returns ``(ok, errors)``.  Everything between polls is sleep: waiting spends
    no context and makes no model invocation, which is the point of doing it
    here rather than inside an agent.
    """
    started, polls, invocations = now(), 0, 0
    while True:
        pull_request = fetch()
        polls += 1
        step = next_step(pull_request)
        elapsed = now() - started
        if step == GONE:
            return WaitResult(GONE, polls=polls, invocations=invocations,
                              elapsed=elapsed)
        if step == REVIEW:
            invocations += 1
            ok, errors = act(pull_request)
            if not ok:
                # Retrying a failed step every poll would spend an invocation
                # each time; the next tick retries it once instead.
                return WaitResult(FAILED, errors=errors, polls=polls,
                                  invocations=invocations, elapsed=elapsed)
            elapsed = now() - started
        if policy.expired(elapsed):
            return WaitResult(EXPIRED, polls=polls, invocations=invocations,
                              elapsed=elapsed)
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

    def act(pull_request):
        rc = ralph_review_round.run_round(story, pull_request,
                                          validated.config, root)
        return rc == 0, [] if rc == 0 else ["review round exited %d" % rc]

    policy = WaitPolicy.from_config(validated.config)
    result = await_review(policy, fetch=fetch, act=act, sleep=time.sleep,
                          now=time.monotonic)
    number = story.get("number", "?")
    if result.kind == EXPIRED:
        print("OK: review window closed after %.0fs on #%s (%d poll%s, %d "
              "invocation%s); a Handoff is due"
              % (result.elapsed, number, result.polls,
                 "" if result.polls == 1 else "s", result.invocations,
                 "" if result.invocations == 1 else "s"))
        return EXIT_WINDOW_EXPIRED
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
