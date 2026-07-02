"""Single-iteration mechanics for the Ralph Loop (US-005, ADR-0003).

A fresh-context iteration takes a chosen story and drives it TDD off-target to a
green local gate: it creates the story branch off base, writes failing tests
from the acceptance criteria (red -> green), tests the logic on the host against
a fake/mock HAL, and runs the configured gating steps before the story counts.
The judgment-heavy TDD is driven by the checked-in agent prompt (prompts/
iterate.v1.md); this module holds the deterministic, host-testable seams the
orchestrator reuses:

  - `branch_name` -- compute the story branch from `branch_pattern` ({issue}/
    {slug}), so the iteration checks out `ralph/<issue#>-slug` off base.
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

DEFAULT_BRANCH_PATTERN = "ralph/{issue}-{slug}"
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
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --branch-name requires a STORY path (or - for stdin)\n")
        return 2
    story_path = rest[0]
    config_path = rest[1] if len(rest) > 1 and rest[1] else None

    pattern = DEFAULT_BRANCH_PATTERN
    if config_path:
        result = ralph_config.load_and_validate(config_path)
        if not result.ok:
            sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
            for err in result.errors:
                sys.stderr.write("  - %s\n" % err)
            return 2
        pattern = result.config["branching"]["branch_pattern"]

    try:
        story = _load_story(story_path)
    except (OSError, ValueError) as exc:
        sys.stderr.write("ralph: could not read story: %s\n" % exc)
        return 2

    print(branch_name(story, pattern))
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
