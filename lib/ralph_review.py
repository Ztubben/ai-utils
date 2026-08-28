"""Ralph-managed pull-request identity.

Automated review is opt-in.  A pull request participates only when its body
contains the exact, versioned marker below; branch names, labels, and title
conventions are deliberately not sufficient because human pull requests may
share any of them.
"""

import json
import re

MANAGED_PR_MARKER = "<!-- ralph-managed-pr:v1 -->"

# A published review is also kept verbatim and machine-readable on the Story, so
# a later process -- the response round, a dispute, an audit -- recovers the
# findings exactly instead of parsing back the Markdown Ralph rendered. It lives
# on the Story issue rather than the pull request because Story comments are
# deliberately excluded from review bundles: a record here can never feed itself
# back into a later reviewer's context.
RESULT_MARKER_TEMPLATE = "<!-- ralph-review-result:v1 head=%s round=%s -->"

# The Implementation Agent's answer to one round, recorded the same way: the
# disposition of every open finding, keyed by its stable identifier, so the loop
# reads what happened instead of inferring it from replies.
RESPONSE_MARKER_TEMPLATE = "<!-- ralph-review-response:v1 head=%s round=%s -->"

# A human's Approve or Request changes, recorded the same way and keyed by the
# review it acted on.  Ralph must act on one native review exactly once -- twice
# would relaunch a model over feedback already answered -- and GitHub offers no
# "handled" flag, so the fact lives on the Story like every other loop fact.
ARBITRATION_MARKER_TEMPLATE = "<!-- ralph-human-arbitration:v1 review=%s -->"

_RECORD_PATTERN = (
    r"<!--\s*%s\s+head=(\S+)\s+round=(\S+)\s*-->\s*```json\s*(.*?)```")
_RESULT_PATTERN = re.compile(_RECORD_PATTERN % "ralph-review-result:v1",
                             re.DOTALL)
_RESPONSE_PATTERN = re.compile(_RECORD_PATTERN % "ralph-review-response:v1",
                               re.DOTALL)
_ARBITRATION_PATTERN = re.compile(
    r"<!--\s*ralph-human-arbitration:v1\s+review=(\S+)\s*-->\s*```json\s*(.*?)```",
    re.DOTALL)

# A published review stamps the exact commit it judged.  That stamp, carried on
# the pull request itself, is what makes "one review invocation per head" hold
# across ticks, machines, and a fresh clone: the answer lives where the review
# does, not in loop-local state that a re-clone would lose.
REVIEW_MARKER_TEMPLATE = "<!-- ralph-review:v1 head=%s -->"


def is_managed_pr(pr):
    """Return whether *pr* carries Ralph's durable review opt-in marker."""
    return MANAGED_PR_MARKER in ((pr or {}).get("body") or "")


def review_marker(head):
    """The durable stamp identifying a Ralph review of exactly *head*."""
    return REVIEW_MARKER_TEMPLATE % head


def review_stamps(pr):
    """Every Ralph review on this pull request, as the head each one judged.

    A list, not a set: one commit can be judged more than once, because a
    dispute answers a round without changing any code.  The number of stamps is
    therefore the number of Negotiation Rounds this pull request has spent.
    """
    prefix, suffix = REVIEW_MARKER_TEMPLATE.split("%s")
    heads = []
    for review in (pr or {}).get("reviews") or []:
        for chunk in ((review or {}).get("body") or "").split(prefix)[1:]:
            if suffix in chunk:
                heads.append(chunk.split(suffix)[0].strip())
    return heads


def reviewed_heads(pr):
    """The commits this pull request already carries a Ralph review for."""
    return set(review_stamps(pr))


def is_reviewed(pr, head):
    """Has a Ralph review already judged *head* on this pull request?"""
    return head in reviewed_heads(pr)


def rounds_reviewed(pr, head):
    """How many Ralph reviews have judged *head*."""
    return review_stamps(pr).count(head)


def rounds_answered(comments, head):
    """How many recorded responses answer a review of *head*."""
    return len(response_records(comments, head))


def needs_review(pr, comments=None):
    """Is a fresh Review Agent round owed for this pull request's head?

    A head is owed one when it has never been judged, and again once the
    Implementation Agent has answered the judgement it did get: a dispute
    changes no code, so the same commit has to be judged a second time -- this
    time against the durable discussion the dispute added -- for the reviewer
    to withdraw or uphold its finding.  Counting answers against reviews bounds
    that: one answer buys exactly one re-review, so a model that only ever
    disputes cannot spin the pull request through unlimited invocations.
    """
    head = (pr or {}).get("headRefOid")
    return rounds_reviewed(pr, head) <= rounds_answered(comments, head)


def _record(template, payload):
    return "%s\n\n```json\n%s\n```" % (
        template % (payload["head"], payload["round"]),
        json.dumps(payload, indent=2, sort_keys=True))


def _records(pattern, comments, head):
    """Every record matching *pattern* for *head*, oldest first.

    Comments are read newest-last, the order gh returns them in, so a later
    round's record follows an earlier one for the same head.
    """
    found = []
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        for recorded_head, _round, payload in pattern.findall(body or ""):
            if recorded_head != head:
                continue
            try:
                found.append(json.loads(payload))
            except ValueError:
                continue
    return found


def _latest(pattern, comments, head):
    """The most recent record matching *pattern* for *head*, or None."""
    records = _records(pattern, comments, head)
    return records[-1] if records else None


def result_record(result):
    """The Story comment body that keeps a validated review result verbatim."""
    return _record(RESULT_MARKER_TEMPLATE, result)


def latest_result(comments, head):
    """The most recent recorded review result for *head*, or None."""
    return _latest(_RESULT_PATTERN, comments, head)


def response_record(response):
    """The Story comment body recording an answer to one round of findings."""
    return _record(RESPONSE_MARKER_TEMPLATE, response)


def latest_response(comments, head):
    """The most recent recorded response to the review of *head*, or None."""
    return _latest(_RESPONSE_PATTERN, comments, head)


def response_records(comments, head):
    """Every recorded response to a review of *head*, oldest first."""
    return _records(_RESPONSE_PATTERN, comments, head)


def result_records(comments, head):
    """Every recorded review result for *head*, oldest first."""
    return _records(_RESULT_PATTERN, comments, head)


def arbitration_record(payload):
    """The Story comment body recording what a human decided, and over what."""
    return "%s\n\n```json\n%s\n```" % (
        ARBITRATION_MARKER_TEMPLATE % payload["review"],
        json.dumps(payload, indent=2, sort_keys=True))


def arbitrations(comments):
    """Every human decision Ralph has already acted on, oldest first."""
    found = []
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        for review_id, payload in _ARBITRATION_PATTERN.findall(body or ""):
            try:
                found.append(json.loads(payload))
            except ValueError:
                found.append({"review": review_id})
    return found


def arbitrated(comments, review_id):
    """Has Ralph already acted on this native review?"""
    return any(record.get("review") == review_id
               for record in arbitrations(comments))


# A change to Ralph's own control plane is held for a human even when every
# model gate is satisfied.  The hold is recorded per head so the notice is
# posted once, not on every poll of the review window.
CONTROL_PLANE_HOLD_TEMPLATE = "<!-- ralph-control-plane-hold:v1 head=%s -->"


def control_plane_hold_record(head, protected):
    """The Story comment recording that this head is held for a human."""
    return "%s\n\n```json\n%s\n```" % (
        CONTROL_PLANE_HOLD_TEMPLATE % head,
        json.dumps({"head": head, "protected": list(protected)}, indent=2,
                   sort_keys=True))


def control_plane_held(comments, head):
    """Has the control-plane notice already been posted for *head*?"""
    marker = CONTROL_PLANE_HOLD_TEMPLATE % head
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        if marker in (body or ""):
            return True
    return False


def negotiation_history(comments):
    """Every recorded round of the negotiation, oldest first.

    Story comments are otherwise kept out of a review bundle -- Handoffs,
    Attempts and session notes live there.  These two record types are the
    exception because they *are* the negotiation: the findings a later round
    adjudicates and the answers given to them, recovered verbatim rather than
    parsed back out of the Markdown they were rendered into.
    """
    history = []
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        for kind, pattern in (("review", _RESULT_PATTERN),
                              ("response", _RESPONSE_PATTERN)):
            for head, round_no, payload in pattern.findall(body or ""):
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    continue
                history.append({"kind": kind, "head": head,
                                "round": round_no, "payload": parsed})
    return history


def previous_findings(comments):
    """The findings of the most recently recorded review, whatever head it judged.

    These are what the next round adjudicates.  Read across heads rather than
    at one: an accepted finding moves the head, so the round that judges the
    fix must still be able to say what happened to the objection behind it.
    """
    reviews = [entry["payload"] for entry in negotiation_history(comments)
               if entry["kind"] == "review"]
    return list((reviews[-1] if reviews else {}).get("findings") or [])


def review_candidates(pull_requests):
    """Return only PRs that opted into automated review.

    Callers must filter before launching a Review Agent, making an unmarked PR
    consume no model invocation rather than merely rejecting it after launch.
    """
    return [pr for pr in (pull_requests or []) if is_managed_pr(pr)]
