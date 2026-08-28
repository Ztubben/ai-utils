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

_RECORD_PATTERN = (
    r"<!--\s*%s\s+head=(\S+)\s+round=(\S+)\s*-->\s*```json\s*(.*?)```")
_RESULT_PATTERN = re.compile(_RECORD_PATTERN % "ralph-review-result:v1",
                             re.DOTALL)
_RESPONSE_PATTERN = re.compile(_RECORD_PATTERN % "ralph-review-response:v1",
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


def reviewed_heads(pr):
    """The commits this pull request already carries a Ralph review for."""
    prefix, suffix = REVIEW_MARKER_TEMPLATE.split("%s")
    heads = set()
    for review in (pr or {}).get("reviews") or []:
        for chunk in ((review or {}).get("body") or "").split(prefix)[1:]:
            if suffix in chunk:
                heads.add(chunk.split(suffix)[0].strip())
    return heads


def is_reviewed(pr, head):
    """Has a Ralph review already judged *head* on this pull request?"""
    return head in reviewed_heads(pr)


def _record(template, payload):
    return "%s\n\n```json\n%s\n```" % (
        template % (payload["head"], payload["round"]),
        json.dumps(payload, indent=2, sort_keys=True))


def _latest(pattern, comments, head):
    """The most recent record matching *pattern* for *head*, or None.

    Comments are read newest-last, the order gh returns them in, so a later
    round's record supersedes an earlier one for the same head.
    """
    found = None
    for comment in comments or []:
        body = comment if isinstance(comment, str) else (comment or {}).get("body")
        for recorded_head, _round, payload in pattern.findall(body or ""):
            if recorded_head != head:
                continue
            try:
                found = json.loads(payload)
            except ValueError:
                continue
    return found


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


def review_candidates(pull_requests):
    """Return only PRs that opted into automated review.

    Callers must filter before launching a Review Agent, making an unmarked PR
    consume no model invocation rather than merely rejecting it after launch.
    """
    return [pr for pr in (pull_requests or []) if is_managed_pr(pr)]
