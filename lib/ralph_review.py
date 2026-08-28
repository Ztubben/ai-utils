"""Ralph-managed pull-request identity.

Automated review is opt-in.  A pull request participates only when its body
contains the exact, versioned marker below; branch names, labels, and title
conventions are deliberately not sufficient because human pull requests may
share any of them.
"""

MANAGED_PR_MARKER = "<!-- ralph-managed-pr:v1 -->"

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


def review_candidates(pull_requests):
    """Return only PRs that opted into automated review.

    Callers must filter before launching a Review Agent, making an unmarked PR
    consume no model invocation rather than merely rejecting it after launch.
    """
    return [pr for pr in (pull_requests or []) if is_managed_pr(pr)]
