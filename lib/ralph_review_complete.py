"""Review-gated completion: the end of the loop (#59).

Everything before this stage has been argument.  This is the stage that acts on
it, and it acts only on facts carried by the *current head*: the CI checks that
head actually ran, and the one model-review context that head actually carries.

A Story with both satisfied merges its **own** pull request into its **own**
base -- the base branch for an Orphan Story, the Feature integration branch for
a Feature Story -- and an AFK Story closes there as Passing.  A HIL Feature
Story merges on exactly the same terms and then moves to Awaiting Bench
Verification, so the Stories after it can build on its code; the physical gate
does not disappear, it moves to the Feature boundary, where a Feature is
refused integration into the base branch while any of its HIL Stories is still
unverified.  A HIL Orphan Story has no Feature branch between it and the base
branch, so it is not merged at all before a human has been to the bench.
Neither path ever targets `main`.
"""
import fnmatch
import json
import os
import posixpath
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_afk  # noqa: E402
import ralph_config  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402
import ralph_review_human  # noqa: E402
import ralph_review_render  # noqa: E402
import ralph_review_round  # noqa: E402
import ralph_story  # noqa: E402

PROTECTED_BRANCH = "main"
IN_REVIEW_LABEL = "state:in-review"
AWAITING_BENCH_LABEL = "state:awaiting-bench"

CHECK_CONTEXT = ralph_review_render.CHECK_CONTEXT

# A check that ran and did not fail. GitHub reports a skipped or neutral check
# with no failure, and a repository is free to have neither.
PASSING_CONCLUSIONS = ("SUCCESS", "NEUTRAL", "SKIPPED")
PASSING_STATES = ("SUCCESS", "EXPECTED")


def protected_paths(changed_paths, patterns):
    """Which of *changed_paths* the declared control-plane patterns cover.

    A bare directory protects everything under it, which is how a repository
    says "all of `schema/`" without thinking about globs; otherwise the pattern
    is an ordinary glob, and `*` deliberately crosses `/` so `prompts/**` means
    what a reader expects it to mean.
    """
    matched = []
    for path in changed_paths or []:
        normalized = posixpath.normpath(path)
        for pattern in patterns or []:
            bare = pattern.rstrip("/")
            if (fnmatch.fnmatch(normalized, pattern)
                    or normalized == bare
                    or normalized.startswith(bare + "/")):
                matched.append(path)
                break
    return matched


def protected_for(pull_request, config, root):
    """The control-plane paths this head touches, per the committed policy.

    Git is only consulted when the target repository actually declares a
    control plane: a repository that protects nothing pays nothing for the
    rule, on every poll of every review window.
    """
    patterns = ((config or {}).get("control_plane") or {}).get("protected") or []
    if not patterns:
        return []
    _diff, changed = ralph_review_context.head_diff(pull_request, root)
    return protected_paths(changed, patterns)


class Gate:
    def __init__(self, ok, errors, ci_green=False, review_ok=False,
                 review_source=None, protected=(), held_for_human=False):
        self.ok = ok
        self.errors = errors
        self.ci_green = ci_green
        self.review_ok = review_ok
        self.review_source = review_source
        self.protected = list(protected)
        self.held_for_human = held_for_human


def _rollup(pull_request):
    return (pull_request or {}).get("statusCheckRollup") or []


def _name(entry):
    return entry.get("context") or entry.get("name") or "(unnamed check)"


def _verdict(entry):
    """Whether one rollup entry is passing, failing, or not finished.

    The rollup mixes two shapes: check runs report `status` plus `conclusion`,
    commit statuses report `state` alone.  Both are read here so a caller never
    has to know which kind produced a given entry.
    """
    if entry.get("conclusion") is not None or entry.get("status") is not None:
        if (entry.get("status") or "COMPLETED") != "COMPLETED":
            return "pending"
        return ("passing" if entry.get("conclusion") in PASSING_CONCLUSIONS
                else "failing")
    state = entry.get("state")
    if state in PASSING_STATES:
        return "passing"
    if state == "PENDING":
        return "pending"
    return "failing"


def gate_for(pull_request, comments, protected=()):
    """Is this head allowed to complete, and if not, why not.

    Two independent halves.  CI is every check the head ran *except* Ralph's own
    review context, which is not CI and is judged separately.  The review half
    is that context reading success -- which an approving model review writes,
    and which a human's Approve writes over the top of -- or, failing that, a
    recorded human approval of this exact commit.  The second path exists
    because a human's decision is authoritative on its own: a status write that
    never landed must not be able to veto it.

    `protected` names the control-plane paths this head touches.  When it is
    non-empty the review half must be satisfied *by the human*, whatever the
    Story's type: a change to the review gate approved by the review gate is
    the mechanism approving changes to itself.
    """
    head = (pull_request or {}).get("headRefOid")
    errors = []
    ci_green = True
    for entry in _rollup(pull_request):
        if _name(entry) == CHECK_CONTEXT:
            continue
        verdict = _verdict(entry)
        if verdict == "passing":
            continue
        ci_green = False
        errors.append("checks/%s: %s" % (_name(entry), verdict))

    review_ok, source = False, None
    for entry in _rollup(pull_request):
        if _name(entry) == CHECK_CONTEXT and _verdict(entry) == "passing":
            review_ok, source = True, "review check"
    human_ok = ralph_review_human.approval_for(comments, head) is not None
    if human_ok:
        review_ok, source = True, "human approval"
    if not review_ok:
        errors.append("checks/%s: the model-review gate is not satisfied for %s"
                      % (CHECK_CONTEXT, head))

    protected = list(protected)
    needs_human = bool(protected) and not human_ok
    if needs_human:
        errors.append(
            "control_plane: %s is protected, so this change needs a native "
            "human approval before it completes, whatever the Story's type"
            % ", ".join(protected))
    ok = ci_green and review_ok and not needs_human
    # The notice is only worth posting once everything else is ready: asking a
    # human to approve work CI has not finished checking wastes their attention
    # on a head that may not survive.
    held = needs_human and ci_green and review_ok
    return Gate(ok, errors, ci_green=ci_green, review_ok=review_ok,
                review_source=source, protected=protected, held_for_human=held)


class Plan:
    def __init__(self, ok, errors, commands, base=None, merged=False,
                 parked=False, gate=None):
        self.ok = ok
        self.errors = errors
        self.commands = commands
        self.base = base
        self.merged = merged
        self.parked = parked
        self.gate = gate


def hold_notice(story, protected):
    """Why this pull request is waiting on a person, in the pull request."""
    return "\n".join([
        "## This change needs a human approval",
        "",
        "Story #%s touches the repository's **protected control plane** — the "
        "parts that govern Ralph's own review gate:" % story.get("number", "?"),
        "",
    ] + ["- `%s`" % path for path in protected] + [
        "",
        "Model review is satisfied and CI is green, but that is deliberately "
        "not enough here: a change to the review mechanism approved by the "
        "review mechanism is the mechanism approving changes to itself. This "
        "holds whatever the Story's type — an AFK Story is not merged and a "
        "HIL Story is not parked for the bench until a person approves.",
        "",
        "**Approve** this pull request to release it. Nothing else is needed; "
        "there is no command to run.",
    ])


def hold_plan(story, pull_request, protected, handle):
    """Say why the change is held, and ask the configured human to look.

    The Story is *not* blocked and not closed: nothing has gone wrong, and the
    negotiation is over. It is simply waiting on the one approval that a model
    is not permitted to give.
    """
    errors = []
    if not ralph_review.is_managed_pr(pull_request):
        errors.append("pull_request: #%s is not Ralph-managed"
                      % (pull_request or {}).get("number", "?"))
    if not protected:
        errors.append("control_plane: nothing protected was touched")
    if not handle:
        errors.append("notify/github: no handle to request a review from")
    if errors:
        return ralph_review_render.Plan(False, errors, [])
    number = pull_request["number"]
    head = pull_request.get("headRefOid")
    return ralph_review_render.Plan(True, [], [
        ["gh", "pr", "comment", str(number), "--body",
         hold_notice(story, protected)],
        ["gh", "pr", "edit", str(number), "--add-reviewer", handle],
        ["gh", "issue", "comment", str(story["number"]), "--body",
         ralph_review.control_plane_hold_record(head, protected)],
    ], head=head)


def completion_plan(story, pull_request, comments, base="develop",
                    afk_merge="squash", prd=None, protected=(),
                    branch_pattern=ralph_iterate.DEFAULT_BRANCH_PATTERN,
                    feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN):
    """The ordered plan that completes one review-approved Story.

    Pure: computes commands, runs nothing.  The branch on Story type is the
    whole point of the stage, and so is the branch on Feature membership
    (ADR-0006): a Feature's code integrates when the Feature merges, never
    story by story, so a Feature Story closes as Passing without a merge and
    leaves its shared pull request open for its siblings.
    """
    errors = []
    if (base or "").strip().lower() == PROTECTED_BRANCH:
        errors.append("branching/base: refusing to complete into main (ADR-0001)")
    if not ralph_review.is_managed_pr(pull_request):
        errors.append("pull_request: #%s is not Ralph-managed; refusing to "
                      "complete it" % (pull_request or {}).get("number", "?"))

    fields = ralph_story.validate_story(story).fields
    kind = fields.get("type")
    if kind not in ("afk", "hil"):
        errors.append("type: completion requires type:afk or type:hil (got %s)"
                      % (kind or "none"))
    if fields.get("state") != "in-review":
        errors.append("state: completion requires state:in-review (got %s)"
                      % (fields.get("state") or "none"))
    topology = None
    try:
        topology = ralph_iterate.resolve_topology(
            story, prd=prd, base=base, branch_pattern=branch_pattern,
            feature_pattern=feature_pattern)
    except ValueError as exc:
        errors.append("branch: %s" % exc)

    gate = gate_for(pull_request, comments, protected=protected)
    if not gate.ok:
        errors.extend(gate.errors)
    if errors:
        return Plan(False, errors, [], base=base, gate=gate)

    number = story["number"]
    head = pull_request.get("headRefOid")
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    target = topology.base
    # The Story's own pull request, merged into the Story's own base, by the
    # strategy the repository configured -- the default squash is what leaves
    # the base one clean commit while the pull request keeps the whole
    # negotiation: every round, every fix, every dispute.
    merge = ["gh", "pr", "merge", str(pull_request["number"]),
             ralph_afk.MERGE_FLAG[afk_merge], "--delete-branch"]

    if kind == "hil":
        # Model review never replaces the bench. The Story parks at the exact
        # commit the human is to verify -- never a moving branch tip, which a
        # sibling Story could move under them. GitHub keeps that commit
        # reachable through the pull request's own ref after the branch is
        # deleted, which is why the anchor names how to fetch it.
        anchor = (
            "Bench anchor: %s\n\nModel review is satisfied (%s) and CI is green, "
            "so #%s is Awaiting Bench Verification. Verify at this exact commit, "
            "not a branch tip:\n\n```sh\ngit fetch origin refs/pull/%s/head\n"
            "git checkout %s\n```\n\nIt is not Passing until you confirm it on "
            "the bench. See the story's ## Bench Test Procedure."
            % (head, gate.review_source, number,
               pull_request.get("number", "?"), head))
        label = ["gh", "issue", "edit", str(number),
                 "--add-label", AWAITING_BENCH_LABEL,
                 "--remove-label", IN_REVIEW_LABEL]
        if parent is None:
            # An Orphan Story's base *is* the base branch, so there is nothing
            # between unverified hardware code and `develop`: it is not merged.
            return Plan(True, [],
                        [["gh", "issue", "comment", str(number), "--body", anchor],
                         label],
                        base=target, parked=True, gate=gate)
        # A Feature Story merges into its Feature branch on the same terms as
        # an AFK one, so the Stories that follow it can build on its code. The
        # gate that used to stand here now stands at the Feature boundary.
        return Plan(True, [],
                    [merge,
                     ["gh", "issue", "comment", str(number), "--body", anchor],
                     label],
                    base=target, merged=True, parked=True, gate=gate)

    integrates = ("Feature #%d" % parent) if parent is not None else target
    return Plan(True, [], [
        merge,
        ["gh", "issue", "close", str(number), "--comment",
         "Model review satisfied (%s) and CI green at %s; merged into %s and "
         "marked Passing (AFK).%s"
         % (gate.review_source, head, target,
            "" if parent is None else
            " It reaches %s when %s merges." % (base, integrates))],
    ], base=target, merged=True, gate=gate)


def fetch_prd(story, root):
    """The Story's PRD issue, when it has one; None for an Orphan Story.

    A Feature Story cannot resolve its working branch without it, so every
    caller would otherwise have to know to fetch it first.
    """
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is None:
        return None
    proc = subprocess.run(
        ["gh", "issue", "view", str(parent), "--json",
         "number,title,labels,body,state"],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode:
        raise RuntimeError(proc.stdout.strip() or "gh issue view failed")
    return json.loads(proc.stdout)


def hold(story, pull_request, config, root, protected):
    """Post the control-plane notice and ask a human; return an exit code."""
    handle = ((config or {}).get("notify") or {}).get("github")
    plan = hold_plan(story, pull_request, protected, handle)
    if not plan.ok:
        sys.stderr.write("REFUSED: control-plane hold\n")
        for error in plan.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    run = ralph_review_render.run_plan(plan.commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: control-plane hold (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        return 1
    print("OK: #%s touches the protected control plane (%s); @%s was asked to "
          "approve PR #%s" % (story["number"], ", ".join(protected), handle,
                              pull_request.get("number", "?")))
    return 0


def complete(story, pull_request, config, root, comments=None, prd=None,
             protected=None):
    """Run the completion against a live checkout; return an exit code."""
    if protected is None:
        try:
            protected = protected_for(pull_request, config, root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the reviewed diff: %s\n" % exc)
            return 2
    if prd is None:
        try:
            prd = fetch_prd(story, root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's PRD: %s\n" % exc)
            return 2
    if comments is None:
        try:
            comments = ralph_review_round.story_comments(story["number"], root)
        except (OSError, ValueError, RuntimeError) as exc:
            sys.stderr.write("ralph: could not read the Story's rounds: %s\n" % exc)
            return 2
    branching = (config or {}).get("branching") or {}
    plan = completion_plan(
        story, pull_request, comments, base=branching.get("base", "develop"),
        afk_merge=branching.get("afk_merge", "squash"), prd=prd,
        protected=protected,
        branch_pattern=branching.get("branch_pattern",
                                     ralph_iterate.DEFAULT_BRANCH_PATTERN),
        feature_pattern=branching.get("feature_pattern",
                                      ralph_iterate.DEFAULT_FEATURE_PATTERN))
    if not plan.ok:
        sys.stderr.write("REFUSED: complete-story\n")
        for error in plan.errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    run = ralph_review_render.run_plan(plan.commands, cwd=root)
    if not run.ok:
        sys.stderr.write("FAILED: complete-story (exit %d): %s\n"
                         % (run.failed.returncode, " ".join(run.failed.args)))
        if run.failed.output.strip():
            sys.stderr.write(run.failed.output.rstrip() + "\n")
        return 1
    if plan.parked:
        print("OK: #%s is Awaiting Bench Verification at %s%s"
              % (story["number"], pull_request.get("headRefOid"),
                 "; merged into %s" % plan.base if plan.merged
                 else "; not merged"))
    else:
        print("OK: #%s merged into %s and closed as Passing"
              % (story["number"], plan.base))
    return 0


def _load(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_complete(rest):
    args, pr_path, prd_path = [], None, None
    i = 0
    while i < len(rest):
        if rest[i] in ("--pr", "--prd"):
            if i + 1 >= len(rest):
                sys.stderr.write("ralph: %s requires a PATH\n" % rest[i])
                return 2
            if rest[i] == "--pr":
                pr_path = rest[i + 1]
            else:
                prd_path = rest[i + 1]
            i += 1
        elif rest[i].startswith("--"):
            sys.stderr.write("ralph: unknown option: %s\n" % rest[i])
            return 2
        else:
            args.append(rest[i])
        i += 1
    if not 1 <= len(args) <= 3:
        sys.stderr.write("usage: ralph --complete-story STORY [CONFIG] [ROOT] "
                         "[--pr PATH] [--prd PATH]\n")
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
        prd = _load(prd_path) if prd_path else None
        if pr_path:
            pull_request, errors = _load(pr_path), []
        else:
            pull_request, errors = ralph_review_round.discover_pull_request(
                story, cwd=root)
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write("ralph: could not read the completion's inputs: %s\n"
                         % exc)
        return 2
    if pull_request is None:
        sys.stderr.write("REFUSED: complete-story\n")
        for error in errors:
            sys.stderr.write("  - %s\n" % error)
        return 2
    return complete(story, pull_request, validated.config, root, prd=prd)


def main(argv):
    if argv and argv[0] == "complete":
        return _cmd_complete(argv[1:])
    sys.stderr.write("usage: ralph_review_complete.py complete STORY [CONFIG] "
                     "[ROOT] [--pr PATH] [--prd PATH]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
