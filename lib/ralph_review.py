"""Ralph-managed pull-request identity.

Automated review is opt-in.  A pull request participates only when its body
contains the exact, versioned marker below; branch names, labels, and title
conventions are deliberately not sufficient because human pull requests may
share any of them.
"""

MANAGED_PR_MARKER = "<!-- ralph-managed-pr:v1 -->"


def is_managed_pr(pr):
    """Return whether *pr* carries Ralph's durable review opt-in marker."""
    return MANAGED_PR_MARKER in ((pr or {}).get("body") or "")


def review_candidates(pull_requests):
    """Return only PRs that opted into automated review.

    Callers must filter before launching a Review Agent, making an unmarked PR
    consume no model invocation rather than merely rejecting it after launch.
    """
    return [pr for pr in (pull_requests or []) if is_managed_pr(pr)]
