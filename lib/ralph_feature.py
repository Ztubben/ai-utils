"""Feature completion pass for the Ralph Loop (US-028, ADR-0006).

When a Feature is complete (all stories closed, PRD open + state:ready), the
completion pass integrates the feature branch into base: (0) check the feature
branch out as origin has it, whatever HEAD was; (1) autosquash-collapse fixup!
commits into their story commits — on rebase conflict, abort and keep the
linear history (cosmetic fallback only); (2) rebase the cleaned feature branch
onto `origin/<base>`; (3) run the full gating steps; (4) push the rebased,
gated branch with lease, so what merges is what was gated; (5) create the
Feature's single PR, require GitHub checks on its published head, and **rebase**-merge it (never a merge commit, never squash),
then (6) close the PRD with a comment and (7) delete the remote branch. The base branch requires linear history, so
GitHub refuses a merge commit; a squash would collapse every Story's commit into
one.

Integration is refused outright while any HIL Story of the Feature is still
open (PRD #69). A HIL Feature Story now merges into the Feature branch before it
is bench-verified, so its successors can build on it; the physical gate that
used to stand at the Story boundary therefore stands here instead. Unverified
hardware code reaches the Feature branch, never the base branch, and the refusal
names every Story a human still has to take to the bench.

A rebase conflict in (2) or a red gate in (3) is a Feature-level blocker: push
the branch as-is, comment the details on the PRD, label it `ready-for-human`,
and stop — the `needs-human` circuit breaker is NOT tripped (independent features
don't poison each other). The same is true of the HIL refusal: it stops this
Feature and nothing else.

The deterministic, host-testable seam mirrors AFK/HIL completion: a pure command
*plan* (`feature_complete_plan`) returns the ordered steps without running
anything, so the merge policy and main-safety guard are unit-testable.
`run_feature_plan` executes a plan step-by-step with fallback handling; the CLI
wrapper (`ralph --complete-feature`) prints and sets exit codes.
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_select  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_complete  # noqa: E402
import ralph_review_human  # noqa: E402
import ralph_review_round  # noqa: E402

DEFAULT_BASE = ralph_iterate.DEFAULT_BASE
READY_FOR_HUMAN_LABEL = "ready-for-human"
MODEL_REVIEW = ralph_review_complete.CHECK_CONTEXT


class Plan:
    def __init__(self, ok, errors, steps, branch=None, base=None):
        self.ok = ok
        self.errors = errors
        self.steps = steps
        self.branch = branch
        self.base = base


class PlanResult:
    def __init__(self, ok, failed_step=None, on_fail_ran=False, pending=False):
        self.ok = ok
        self.failed_step = failed_step
        self.on_fail_ran = on_fail_ran
        self.pending = pending


def unverified_hil_stories(prd, raw_backlog):
    """The Feature's HIL Stories that no human has bench-verified yet.

    Open is the whole test: a HIL Story is closed by the human who verified it
    on the bench, so an open one is unverified by definition -- whether it is
    Awaiting Bench Verification, still In Review, or blocked.  Ascending, so a
    refusal reads the same way twice.
    """
    stories = ralph_select.normalize(raw_backlog)
    return sorted(
        story["number"] for story in stories
        if not story.get("is_prd") and not story.get("closed")
        and story.get("parent") == prd.get("number")
        and story.get("type") == "hil" and story["number"] is not None)


def feature_stories(prd, raw_backlog):
    """Every Story of this Feature, open or closed, ascending.

    These are the Stories whose reviews the Feature pull request inherits
    (#172); None when the backlog was not read, which the inheritance check
    refuses rather than reading as "a Feature of no Stories".
    """
    if raw_backlog is None:
        return None
    return sorted(
        story["number"] for story in ralph_select.normalize(raw_backlog)
        if not story.get("is_prd") and story.get("parent") == prd.get("number")
        and story["number"] is not None)


class Inheritance:
    def __init__(self, ok, errors, sources):
        self.ok = ok
        self.errors = errors
        # The Story pull requests whose reviews the Feature head inherits.
        self.sources = sources


def _review_of(pull_request, comments):
    """What reviewed this merged Story pull request's head, if anything."""
    for entry in pull_request.get("statusCheckRollup") or []:
        if (ralph_review_complete._name(entry) == MODEL_REVIEW
                and ralph_review_complete._verdict(entry) == "passing"):
            return "model review"
    if ralph_review_human.approval_for(comments, pull_request.get("headRefOid")):
        return "human approval"
    return None


def inheritance_verdict(stories, pulls, comments, commits):
    """May the Feature head inherit its Stories' model reviews? (#172)

    Pure. *stories* are the Feature's Story numbers; *pulls* its merged,
    Ralph-managed Story pull requests (each carrying the ``patch_id`` of its
    diff); *comments* each Story's comments, where a human approval is
    recorded; *commits* the rebased Feature branch's commits (``sha``,
    ``subject``, ``patch_id``), oldest first.

    Inheritance holds only when integration changed nothing: every Story
    merged through a pull request whose merged head was reviewed, and every
    Feature commit carries exactly the patch of one such reviewed change. A
    commit no Story pull request carried and a Story change the rebase onto
    the base altered look the same from here -- a patch nobody reviewed -- and
    both are named.
    """
    if stories is None:
        return Inheritance(False, [
            "backlog: the Feature's Stories were not read, so no review can "
            "be inherited"], [])
    errors = []
    reviewed = {}  # patch-id -> Story pull request number
    for number in stories:
        own = [pr for pr in pulls
               if ralph_review_round.references_story(pr, number)]
        if not own:
            errors.append("story/#%d: merged through no Ralph-managed pull "
                          "request" % number)
            continue
        passed = [pr for pr in own
                  if _review_of(pr, comments.get(number) or [])]
        if not passed:
            errors.append(
                "story/#%d: merged through %s, whose merged head carries "
                "neither a passing %s nor a recorded human approval"
                % (number, ", ".join("#%s at %s" % (
                    pr["number"], (pr.get("headRefOid") or "?")[:7])
                    for pr in own), MODEL_REVIEW))
            continue
        for pr in passed:
            if pr.get("patch_id"):
                reviewed.setdefault(pr["patch_id"], pr["number"])
    sources = []
    for commit in commits:
        source = reviewed.get(commit.get("patch_id"))
        if source is None:
            errors.append(
                "commit/%s \"%s\": its patch is no reviewed Story pull "
                "request's merged change (no Story pull request carried it, "
                "or the rebase onto the base altered it)"
                % (commit["sha"][:7], commit.get("subject", "")))
        elif source not in sources:
            sources.append(source)
    if errors:
        return Inheritance(False, errors, [])
    return Inheritance(True, [], sorted(sources))


def inherited_description(sources):
    """The status description naming the Story pull requests inherited from."""
    return ("Inherited from Story PRs %s"
            % ", ".join("#%s" % n for n in sources))[:140]


def _patch_id(diff, cwd):
    proc = subprocess.run(["git", "patch-id", "--stable"], input=diff, cwd=cwd,
                          capture_output=True, text=True, check=True)
    return (proc.stdout.split() or [None])[0]


def gather_inheritance(plan, stories, cwd):
    """Read what `inheritance_verdict` judges: merged Story pull requests into
    the Feature branch, their diffs' patch-ids, and the rebased Feature's
    commits. Raises CalledProcessError/ValueError when GitHub or git cannot
    answer -- an unread fact is never an inherited review."""
    def run(args):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              check=True).stdout

    pulls = []
    if stories:
        listed = json.loads(run(
            ["gh", "pr", "list", "--state", "merged", "--base", plan.branch,
             "--limit", "100", "--json",
             "number,body,headRefOid,statusCheckRollup"]) or "[]")
        for pr in ralph_review.review_candidates(listed):
            if not any(ralph_review_round.references_story(pr, n)
                       for n in stories):
                continue
            pr["patch_id"] = _patch_id(
                run(["gh", "pr", "diff", str(pr["number"]), "--color", "never"]),
                cwd)
            pulls.append(pr)
    comments = {}
    for number in stories or []:
        own = [pr for pr in pulls if ralph_review_round.references_story(pr, number)]
        if own and not any(_review_of(pr, []) for pr in own):
            comments[number] = ralph_review_round.story_comments(number, cwd)
    return inheritance_verdict(stories, pulls, comments,
                               feature_commits(plan.base, plan.branch, cwd))


def feature_commits(base, branch, cwd):
    """The rebased Feature branch's own commits, oldest first, with patch-ids.

    Read from the local Feature ref against the base as origin has it -- the
    head the pass gated and pushed, never the checkout's HEAD.
    """
    def run(args):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              check=True).stdout

    commits = []
    log = run(["git", "log", "--reverse", "--no-merges", "--format=%H%x09%s",
               "origin/%s..refs/heads/%s" % (base, branch)])
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        commits.append(dict(sha=sha, subject=subject, patch_id=_patch_id(
            run(["git", "show", "--no-color", "--format=", sha]), cwd)))
    return commits


def feature_complete_plan(prd, base=DEFAULT_BASE,
                          feature_pattern=ralph_iterate.DEFAULT_FEATURE_PATTERN,
                          gating=None, backlog=None):
    """Build the ordered step plan to integrate a complete Feature into base.

    Pure: computes steps, runs nothing. Each step is a dict with:
      - name: step identifier
      - commands: list of argv lists to run in order
      - fallback_ok (optional): if True, step failure is tolerated (cosmetic)
      - on_fail (optional): list of argv lists to run on failure (blocker path)

    Refuses (ok=False, no steps) when *backlog* shows
    any HIL Story of this Feature still open -- naming each one, because the
    person reading the refusal is the person who has to bench-verify them.
    Pass `backlog=None` only where the backlog genuinely cannot be read; the
    live CLI reads it and refuses rather than integrating unchecked.
    """
    errors = []

    unverified = (unverified_hil_stories(prd, backlog)
                  if backlog is not None else [])
    if unverified:
        errors.append(
            "hil: Feature #%s has unverified HIL %s (%s); a Feature is not "
            "integrated into %s until every one of them is bench-verified and "
            "closed" % (prd.get("number"),
                        "Story" if len(unverified) == 1 else "Stories",
                        ", ".join("#%d" % number for number in unverified),
                        base))

    if errors:
        return Plan(False, errors, [], base=base)

    branch = ralph_iterate.branch_name(prd, feature_pattern)
    prd_number = prd["number"]

    remote_base = "origin/" + base
    ref = "refs/heads/" + branch
    # Publication names both refs (AGENTS.md): the checkout's HEAD is not
    # evidence of which branch is the Feature's, and `-u` must not move its
    # upstream. With lease, so a branch someone pushed to since the sync step
    # fetched it is never overwritten.
    push = ["git", "push", "--force-with-lease=" + ref, "origin", "%s:%s" % (ref, ref)]

    # Blocker commands: push the branch, comment on PRD, label ready-for-human.
    # These are shared by the rebase and gating failure paths.
    def _blocker_commands(reason_placeholder):
        return [
            push,
            ["gh", "issue", "comment", str(prd_number),
             "--body", "Feature completion blocked: %s on `%s`. "
             "Branch pushed as-is for human triage." % (reason_placeholder, branch)],
            ["gh", "issue", "edit", str(prd_number),
             "--add-label", READY_FOR_HUMAN_LABEL],
        ]

    steps = [
        # Step 0: put the checkout on the Feature branch as origin has it
        # (#120). Inside a tick HEAD is whatever the last iteration left, and
        # the pass used to rebase, gate and push *that*.
        {
            "name": "sync",
            "commands": [
                ["git", "fetch", "origin"],
                ["git", "checkout", "-q", "--no-track", "-B", branch, "origin/" + branch],
            ],
        },
        # Step 1: autosquash-collapse fixup! commits
        {
            "name": "autosquash",
            "commands": [
                # Interactive with a no-op editor: before git 2.44 a plain
                # `rebase --autosquash` ignores the flag and squashes nothing.
                ["git", "-c", "sequence.editor=:", "rebase", "-i", "--autosquash",
                 remote_base],
            ],
            "fallback_ok": True,  # conflict -> keep linear history, continue
            "abort_on_fail": [
                ["git", "rebase", "--abort"],
            ],
        },
        # Step 2: rebase onto the base as origin has it -- the local base
        # branch is never fetched into and may be arbitrarily stale.
        {
            "name": "rebase",
            "commands": [
                ["git", "rebase", remote_base],
            ],
            "on_fail": _blocker_commands("rebase conflict"),
            "abort_on_fail": [
                ["git", "rebase", "--abort"],
            ],
        },
        # Step 3: run full gating
        {
            "name": "gating",
            "commands": [],  # filled by the runner from the gating config
            "gating_steps": gating or [],
            "on_fail": _blocker_commands("gating failure"),
        },
        # Step 4: publish what was gated, so that is what the PR merges (#120).
        # Without it the PR merged the remote branch as it was, and the
        # autosquashed fixups were lost.
        {
            "name": "push",
            "commands": [push],
        },
        # Step 5: find/create the PR, wait for required GitHub checks, and
        # rebase-merge the verified head (linear history, per-Story commits). No --delete-branch: the branch is checked out here, and
        # gh deletes the local branch too, which fails after the merge.
        {
            "name": "pr-create",
            "required_checks": [g["name"] for g in gating or []] + [MODEL_REVIEW],
            "prd_number": prd_number,
            "stories": feature_stories(prd, backlog),
            "commands": [
                ["gh", "pr", "create", "--base", base, "--head", branch,
                 "--title", prd.get("title", "Feature #%d" % prd_number),
                 "--body", "Merges Feature #%d into %s.\n\nCloses #%d"
                 % (prd_number, base, prd_number)],
                ["gh", "pr", "merge", branch, "--rebase",
                 "--match-head-commit", "{head}"],
            ],
        },
        # Step 6: close the PRD
        {
            "name": "prd-close",
            "commands": [
                ["gh", "issue", "close", str(prd_number),
                 "--comment", "Feature integrated into %s via rebase; "
                 "PRD closed." % base],
            ],
        },
        # Step 7: delete the remote Feature branch by name. Best-effort: the
        # repository may already have deleted it on merge.
        {
            "name": "cleanup",
            "commands": [
                ["git", "push", "origin", "--delete", ref],
            ],
            "fallback_ok": True,
        },
    ]

    return Plan(True, [], steps, branch=branch, base=base)


def required_check_verdict(pull_request, required):
    """Read required contexts on one PR head; absent checks must wait."""
    rollup = pull_request.get("statusCheckRollup") or []
    failing, pending = [], []
    for name in dict.fromkeys(required):
        entries = [e for e in rollup if (e.get("context") or e.get("name")) == name]
        # Required checks must explicitly succeed, including model review.
        verdicts = [ralph_review_complete._verdict(e) for e in entries]
        if "failing" in verdicts:
            failing.append(name)
        elif not entries or "pending" in verdicts or any(
                (e.get("conclusion") or e.get("state")) != "SUCCESS" for e in entries):
            pending.append(name)
    return failing, pending


def inheritance_status(head, sources):
    """Write the inherited model review on exactly the Feature head (#172)."""
    return ["gh", "api", "--method", "POST",
            "repos/{owner}/{repo}/statuses/%s" % head,
            "-f", "state=success",
            "-f", "context=" + MODEL_REVIEW,
            "-f", "description=" + inherited_description(sources)]


def _checked_feature_pr(plan, step, cwd):
    """Find/create the PR, then merge only the exact published, checked head.

    The Feature pull request is never re-reviewed (#172): its
    `ralph/model-review` is inherited from its Story pull requests, which this
    pass confirms against the rebased Feature branch before writing the context
    itself -- on this head and no other.
    """
    def run(args):
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout

    def block(body):
        run(["gh", "issue", "comment", str(step["prd_number"]), "--body", body])
        run(["gh", "issue", "edit", str(step["prd_number"]),
             "--add-label", READY_FOR_HUMAN_LABEL])
        return PlanResult(False, failed_step="github-checks", on_fail_ran=True)

    try:
        head = run(["git", "rev-parse", "--verify", "refs/heads/" + plan.branch]).strip()
        if not head:
            raise ValueError("missing Feature head")
        prs = json.loads(run(["gh", "pr", "list", "--state", "open",
                              "--head", plan.branch, "--base", plan.base, "--json", "number"]))
        if not prs:
            run(step["commands"][0])
        pr = json.loads(run(["gh", "pr", "view", str(prs[0]["number"]) if prs else plan.branch,
                             "--json", "headRefOid,statusCheckRollup,url"]))
        if pr.get("headRefOid") != head:
            return PlanResult(True, pending=True)
        failing, pending = required_check_verdict(pr, step["required_checks"])
        if failing:
            return block("Feature completion blocked: failing checks %s on %s (head %s)."
                         % (", ".join(failing), pr["url"], head))
        inherited = gather_inheritance(plan, step.get("stories"), cwd)
        if not inherited.ok:
            return block(
                "Feature completion blocked: %s on %s (head %s) cannot inherit "
                "its Story pull requests' model reviews. The Feature pull "
                "request is never re-reviewed, so every commit on it must be "
                "the unchanged counterpart of a reviewed Story's merged "
                "change:\n\n%s\n\nHIL triage required: take the named work "
                "through a reviewed Story pull request (or review this head "
                "yourself), then remove ready-for-human from this PRD to retry."
                % (MODEL_REVIEW, pr["url"], head,
                   "\n".join("- " + error for error in inherited.errors)))
        if MODEL_REVIEW in pending:
            run(inheritance_status(head, inherited.sources))
            pending.remove(MODEL_REVIEW)
        if pending:
            return PlanResult(True, pending=True)
        run([head if arg == "{head}" else arg for arg in step["commands"][1]])
        return PlanResult(True)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError, TypeError):
        return PlanResult(False, failed_step="pr-create")


def run_feature_plan(plan, cwd=None, dry_run=False, inject_failures=None):
    """Execute a feature completion plan step-by-step.

    Each step's commands are run in order. On failure:
      - If the step has `fallback_ok`, abort (if abort_on_fail is present) and
        continue to the next step.
      - If the step has `on_fail`, run those commands and return a failed result.
      - Otherwise, return a failed result immediately.

    dry_run: if True, don't actually execute anything (for plan-level testing).
    inject_failures: set of step names that should simulate failure (testing only).
    """
    inject_failures = inject_failures or set()

    for step in plan.steps:
        name = step["name"]
        failed = name in inject_failures

        if not dry_run and not failed:
            if name == "pr-create":
                result = _checked_feature_pr(plan, step, cwd)
                if not result.ok or result.pending:
                    return result
                continue
            # Local gating and GitHub checks are independent requirements.
            if name == "gating" and step.get("gating_steps"):
                gres = ralph_iterate.run_gating(step["gating_steps"], cwd=cwd)
                failed = not gres.ok
            else:
                for args in step["commands"]:
                    proc = subprocess.run(
                        args, cwd=cwd,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                    if proc.returncode != 0:
                        failed = True
                        break

        if failed:
            # Run abort commands if present (e.g. git rebase --abort)
            if step.get("abort_on_fail") and not dry_run:
                for args in step["abort_on_fail"]:
                    subprocess.run(args, cwd=cwd,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            if step.get("fallback_ok"):
                # Cosmetic failure (autosquash): continue
                continue

            # Blocker: run on_fail commands and stop
            on_fail_ran = False
            if step.get("on_fail"):
                on_fail_ran = True
                if not dry_run:
                    for args in step["on_fail"]:
                        subprocess.run(args, cwd=cwd,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            return PlanResult(False, failed_step=name, on_fail_ran=on_fail_ran)

    return PlanResult(True)


def _load_prd(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_complete(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --complete-feature requires a PRD path (or - for stdin)\n")
        return 2
    prd_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else ".ralph.yml"

    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2
    branching = result.config["branching"]
    gating = result.config["gating"]

    try:
        prd = _load_prd(prd_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read PRD: %s\n" % exc)
        return 2

    # The config-level refusals are settled before the backlog is read: a
    # Feature that may not be integrated at all is not worth a gh call.
    plan = feature_complete_plan(
        prd, base=branching["base"],
        feature_pattern=branching["feature_pattern"], gating=gating)
    if plan.ok:
        # Fails closed: a backlog that cannot be read is not permission to
        # integrate a Feature whose HIL Stories may be unverified.
        try:
            backlog = ralph_select._scan_gh()
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            sys.stderr.write("REFUSED: feature completion\n"
                             "  - backlog: could not read the Feature's "
                             "Stories, so its HIL Stories cannot be checked: "
                             "%s\n" % exc)
            return 2
        plan = feature_complete_plan(
            prd, base=branching["base"],
            feature_pattern=branching["feature_pattern"],
            gating=gating, backlog=backlog)
    if not plan.ok:
        sys.stderr.write("REFUSED: feature completion\n")
        for err in plan.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    run = run_feature_plan(plan, cwd=os.getcwd())
    if run.pending:
        print("PENDING: Feature #%s waiting for GitHub checks" % prd["number"])
        return 0
    if run.ok:
        print("OK: Feature #%s integrated into %s (rebased); PRD closed"
              % (prd["number"], plan.base))
        return 0
    sys.stderr.write("BLOCKED: feature completion at step '%s'\n" % run.failed_step)
    if run.on_fail_ran:
        sys.stderr.write("  branch pushed, PRD commented + labeled ready-for-human\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_feature.py complete <prd.json | -> [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "complete":
        return _cmd_complete(rest)
    sys.stderr.write("ralph_feature.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
