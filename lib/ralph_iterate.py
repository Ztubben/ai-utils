"""Single-iteration mechanics for the Ralph Loop (US-005, ADR-0003).

A fresh-context iteration takes a chosen story and drives it TDD off-target to a
green local gate: it creates the story branch off base, writes failing tests
from the acceptance criteria (red -> green), tests the logic on the host against
a fake/mock HAL, and runs the configured gating steps before the story counts.
The judgment-heavy TDD is driven by the checked-in agent prompt (prompts/
iterate.v1.md); this module holds the deterministic, host-testable seams the
orchestrator reuses:

  - `branch_name` -- compute a branch from a pattern ({issue}/{slug}).
  - `resolve_topology` -- resolve the two names one story has (ADR-0006, as
    amended): the working branch it commits on, and the base its pull request
    targets. Every story works on its own story branch via `branch_pattern`;
    only the base differs, an Orphan Story's being the configured base branch
    and a Feature story's its Feature integration branch via `feature_pattern`,
    named from the PRD issue. `resolve_branch` is the working-branch half.
  - `run_gating` -- run the configured gating steps locally, fail-fast, and keep
    output low-verbosity (only a failed step's output is surfaced).

Pure/side-effect-light: `branch_name`/`slugify` are pure; `run_gating` shells out
to the configured commands but returns a result object rather than exiting. The
CLI wrappers (`ralph --branch-name`, `ralph --run-gating`) print and set exit
codes.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_story  # noqa: E402

DEFAULT_BRANCH_PATTERN = "ralph/{issue}-{slug}"
DEFAULT_FEATURE_PATTERN = "feature/{issue}-{slug}"
DEFAULT_BASE = "develop"
SLUG_MAX = 50


def slugify(title):
    """Lowercase, replace runs of non-alphanumerics with a single dash, trim.

    Truncated to SLUG_MAX chars with no trailing dash so branch names stay tidy.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    if len(slug) > SLUG_MAX:
        slug = slug[:SLUG_MAX].rstrip("-")
    return slug


def branch_name(story, pattern=DEFAULT_BRANCH_PATTERN):
    """Compute the story branch name by substituting {issue} and {slug}."""
    return pattern.replace("{issue}", str(story["number"])).replace(
        "{slug}", slugify(story.get("title", ""))
    )


class Topology:
    """The two branch names one story resolves to (ADR-0006, as amended).

    `branch` is the story's own working branch; `base` is what its pull request
    targets. The topology is uniform across story kinds and only the base
    differs, which is why the two are resolved together: a caller that computed
    them apart could push to one story's branch and open its pull request
    against another story's base.

    `feature` is the Feature integration branch when the story has one, and
    None for an Orphan Story -- the same value as `base` in the Feature case,
    named separately so a caller can ask "does this story have a Feature
    branch to create?" without re-deriving it.
    """

    def __init__(self, branch, base, feature=None):
        self.branch = branch
        self.base = base
        self.feature = feature


def resolve_topology(story, prd=None, base=DEFAULT_BASE,
                     branch_pattern=DEFAULT_BRANCH_PATTERN,
                     feature_pattern=DEFAULT_FEATURE_PATTERN):
    """Resolve the working branch and the pull-request base for one story.

    Every story -- Orphan or Feature -- works on its own story branch, named
    from `branch_pattern` over the **story** issue. An Orphan Story's pull
    request targets the configured base branch; a Feature story's targets its
    Feature integration branch, named from `feature_pattern` over the **PRD**
    issue (same slug rules). Both are deterministic: recomputable identically
    at every stage from the backlog alone.

    Raises ValueError when a Feature story is given without its PRD context, or
    when the supplied PRD does not match the story's `Parent:` line -- the base
    cannot be named without it, and guessing one would open the pull request
    somewhere nobody is looking.
    """
    branch = branch_name(story, branch_pattern)
    _, parent = ralph_story._parse_parent(story.get("body") or "")
    if parent is None:
        return Topology(branch, base)
    if prd is None:
        raise ValueError(
            "story #%s is a Feature story (Parent: #%d); its PRD issue is "
            "required to resolve the feature branch" % (story.get("number"), parent))
    if prd.get("number") != parent:
        raise ValueError(
            "PRD #%s does not match story #%s's Parent: #%d"
            % (prd.get("number"), story.get("number"), parent))
    feature = branch_name(prd, feature_pattern)
    return Topology(branch, feature, feature=feature)


def resolve_branch(story, prd=None, branch_pattern=DEFAULT_BRANCH_PATTERN,
                   feature_pattern=DEFAULT_FEATURE_PATTERN):
    """The working branch an iteration commits on -- the topology's first half.

    Kept as its own name because most callers want only this one; it still
    refuses a Feature story with no PRD, so a caller cannot resolve half a
    topology and discover the other half is unresolvable later.
    """
    return resolve_topology(story, prd=prd, branch_pattern=branch_pattern,
                            feature_pattern=feature_pattern).branch


class StepResult:
    def __init__(self, name, run, returncode, output):
        self.name = name
        self.run = run
        self.returncode = returncode
        self.output = output
        self.ok = returncode == 0


class GatingResult:
    def __init__(self, steps):
        self.steps = steps
        self.failed = next((s for s in steps if not s.ok), None)
        self.ok = self.failed is None


def run_gating(steps, cwd=None):
    """Run the configured gating steps in order, stopping at the first failure.

    Each step's combined stdout+stderr is captured (kept low-verbosity: the CLI
    surfaces output only for a failing step). Returns a GatingResult.
    """
    results = []
    for step in steps:
        proc = subprocess.run(
            step["run"], shell=True, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        results.append(StepResult(step["name"], step["run"], proc.returncode, proc.stdout))
        if proc.returncode != 0:
            break
    return GatingResult(results)


def _load_story(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def _cmd_branch_name(rest):
    # `--base` is a flag rather than a second output line: the prompt and the
    # tick both read this command's whole stdout as one branch name, so adding
    # a line to the default output would rename every working branch at once.
    want_base = "--base" in rest
    rest = [arg for arg in rest if arg != "--base"]
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --branch-name requires a STORY path (or - for stdin)\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else None
    prd_path = rest[2] if len(rest) > 2 and rest[2] else None

    branch_pattern = DEFAULT_BRANCH_PATTERN
    feature_pattern = DEFAULT_FEATURE_PATTERN
    base = DEFAULT_BASE
    if config_path:
        result = ralph_config.load_and_validate(config_path)
        if not result.ok:
            sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
            for err in result.errors:
                sys.stderr.write("  - %s\n" % err)
            return 2
        branch_pattern = result.config["branching"]["branch_pattern"]
        feature_pattern = result.config["branching"]["feature_pattern"]
        base = result.config["branching"]["base"]

    try:
        story = _load_story(story_path)
        prd = _load_story(prd_path) if prd_path else None
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    try:
        topology = resolve_topology(
            story, prd=prd, base=base, branch_pattern=branch_pattern,
            feature_pattern=feature_pattern)
    except ValueError as exc:
        sys.stderr.write("ralph: %s\n" % exc)
        return 2
    print(topology.base if want_base else topology.branch)
    return 0


def _cmd_run_gating(rest):
    config_path = rest[0] if rest and rest[0] else ".ralph.yml"
    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        return 2

    gres = run_gating(result.config["gating"], cwd=os.getcwd())
    for step in gres.steps:
        if step.ok:
            print("✓ %s" % step.name)
        else:
            sys.stderr.write("✗ %s (exit %d)\n" % (step.name, step.returncode))
            if step.output.strip():
                sys.stderr.write(step.output.rstrip() + "\n")
    if gres.ok:
        print("OK: gating passed (%d steps)" % len(gres.steps))
        return 0
    sys.stderr.write("FAILED: gating\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_iterate.py {branch-name|run-gating} ...\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "branch-name":
        return _cmd_branch_name(rest)
    if mode == "run-gating":
        return _cmd_run_gating(rest)
    sys.stderr.write("ralph_iterate.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
