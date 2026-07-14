"""Pure story-selection engine for the Ralph Loop (US-004, ADR-0002).

Given the superproject backlog (GitHub issues in `gh --json` shape), decide the
single next action Ralph should take -- resume an in-progress story, start a
ready one, do nothing, or halt -- and change nothing. The decision is a pure
function over a normalized story list (no network, no LLM) so the crown-jewel
logic stays deterministic and unit-testable; the CLI wrapper (`ralph --dry-run`)
does the live gh scan and the printing.

Rules (ADR-0002, CONTEXT.md):
  - Resume-first: any state:in-progress story is chosen before any state:ready
    scan (a prior iteration checkpointed it via Handoff).
  - Ordering: key is (prio, afk-before-hil, issue#) ascending. Within the same
    prio:N -- and among prio-less stories -- type:afk sorts ahead of type:hil,
    with lowest issue number (FIFO) as the final tiebreak. An explicit prio:N
    still wins absolutely over any type ranking. prio is optional (ADR-0002):
    a story with no prio:N sorts as lowest priority (prio = +inf).
  - state:blocked stories and design-decision Blockers (ready-for-human, kept
    out of state:ready) are skipped.
  - Dependencies: a `Depends on: #N` edge is satisfied only when #N is Passing,
    i.e. closed -- an AFK dep only once merged, a HIL dep only once bench-
    verified; both surface as the referenced issue being closed. A story blocked
    by an unverified HIL dependency (still open, e.g. state:awaiting-bench) is
    never selected.
  - PRDs and Features (ADR-0002 amendments, ADR-0006): a `prd`-labeled issue is
    never selected as work, whatever state:* it carries. A Feature story
    inherits its PRD's unsatisfied `Depends on:` edges. Reachability: a closed
    Feature story means "on its unmerged feature branch", not "in the base
    branch", so a dependency edge onto a Feature story is satisfiable only from
    a same-Feature story -- from another Feature's story or an Orphan Story it
    is never satisfied. Deps on Orphan Stories and PRDs keep closed-means-
    satisfied (a closed PRD means the whole Feature merged into base).
  - A `needs-human` label anywhere in the open backlog means the circuit breaker
    tripped: the loop halts.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_story  # noqa: E402

NEEDS_HUMAN_LABEL = "needs-human"

RESUME = "resume"
START = "start"
NO_WORK = "no-work"
HALT = "halt"


class Action:
    def __init__(self, kind, story=None, reason=""):
        self.kind = kind
        self.story = story
        self.number = story["number"] if story else None
        self.reason = reason

    def __repr__(self):
        if self.number is not None:
            return "Action(%s #%s)" % (self.kind, self.number)
        return "Action(%s)" % self.kind


def normalize(raw_issues):
    """Turn `gh --json` issue dicts into the flat records the engine consumes.

    Reuses the story-format field extraction (labels + Depends on: parsing) and
    adds the gh open/closed state (`closed`) and the `needs-human` flag. Pure.
    """
    stories = []
    for raw in raw_issues:
        fields = dict(ralph_story.validate_story(raw).fields)
        names = ralph_story._label_names(raw)
        gh_state = raw.get("state") or "OPEN"
        fields["closed"] = str(gh_state).upper() == "CLOSED"
        fields["needs_human"] = NEEDS_HUMAN_LABEL in names
        stories.append(fields)
    return stories


def _order_key(story):
    prio = story["prio"] if story["prio"] is not None else float("inf")
    type_rank = 0 if story["type"] == "afk" else 1
    number = story["number"] if story["number"] is not None else float("inf")
    return (prio, type_rank, number)


def _is_feature_story(story):
    """A Feature story has a parent PRD; a PRD or an Orphan Story does not."""
    return not story.get("is_prd") and story.get("parent") is not None


def _effective_deps(story, by_number):
    """The story's own `Depends on:` edges plus the ones inherited from its
    PRD (ADR-0002 amended): a Feature story is ineligible while its PRD's
    cross-Feature ordering deps are unsatisfied. A parent absent from the
    scanned backlog contributes nothing.
    """
    deps = list(story["depends_on"])
    parent = story.get("parent")
    if parent is not None:
        prd = by_number.get(parent)
        if prd is not None:
            deps.extend(d for d in prd["depends_on"] if d not in deps)
    return deps


def _dep_satisfied(story, target):
    """Closed-means-satisfied, gated by reachability (ADR-0006): a closed
    Feature story lives on its unmerged feature branch, so only a same-Feature
    dependent can reach its code -- from another Feature's story or an Orphan
    Story the edge is never satisfied. Orphan Stories and PRDs close by
    merging into the base branch, reachable from anywhere.
    """
    if _is_feature_story(target) and story.get("parent") != target.get("parent"):
        return False
    return bool(target.get("closed"))


def _deps_satisfied(story, by_number):
    """A `Depends on:` edge (own or inherited from the PRD) is satisfied only
    when the referenced issue is Passing (closed) *and* its code is reachable
    from the dependent. A dep absent from the scanned backlog is treated as
    already done. Any open dependency (ready/in-progress/awaiting-bench/
    blocked) -- including an unverified HIL story -- leaves the dependent
    ineligible.
    """
    for dep in _effective_deps(story, by_number):
        target = by_number.get(dep)
        if target is None:
            continue
        if not _dep_satisfied(story, target):
            return False
    return True


def select_next(stories):
    """Pure selection over a normalized story list. Returns an Action."""
    open_stories = [s for s in stories if not s.get("closed")]

    if any(s.get("needs_human") for s in open_stories):
        return Action(HALT, reason="needs-human: loop halted, awaiting human")

    # A prd-labeled issue is never work (ADR-0002): whatever state:* it
    # carries, it must not come back as a start or resume action.
    open_stories = [s for s in open_stories if not s.get("is_prd")]

    in_progress = [s for s in open_stories
                   if s["state"] == "in-progress" and not s["is_blocker"]]
    if in_progress:
        return Action(RESUME, sorted(in_progress, key=_order_key)[0])

    by_number = {s["number"]: s for s in stories if s["number"] is not None}
    ready = [s for s in open_stories
             if s["state"] == "ready" and not s["is_blocker"]
             and _deps_satisfied(s, by_number)]
    if ready:
        return Action(START, sorted(ready, key=_order_key)[0])

    return Action(NO_WORK, reason="no eligible ready or in-progress stories")


def next_action(raw_issues):
    """Convenience: normalize a raw gh backlog then select the next action."""
    return select_next(normalize(raw_issues))


def _scan_gh():
    out = subprocess.run(
        ["gh", "issue", "list", "--state", "all", "--limit", "1000",
         "--json", "number,title,labels,body,state"],
        stdout=subprocess.PIPE, check=True, text=True).stdout
    return json.loads(out or "[]")


def _load_backlog(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def main(argv):
    path = None
    if argv:
        if argv[0] in ("-h", "--help"):
            sys.stderr.write("usage: ralph_select.py [BACKLOG_JSON | -]\n")
            return 2
        path = argv[0]
    try:
        raw = _load_backlog(path) if path is not None else _scan_gh()
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read backlog: %s\n" % exc)
        return 2
    except subprocess.CalledProcessError as exc:
        sys.stderr.write("ralph: gh scan failed: %s\n" % exc)
        return 2

    action = next_action(raw)
    if action.number is not None:
        print("%s #%s" % (action.kind, action.number))
    else:
        print(action.kind)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
