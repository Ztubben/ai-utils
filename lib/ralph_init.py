"""One-shot superproject bootstrap for the Ralph Loop (`ralph --init`).

Ralph reads and writes the backlog through GitHub labels (ADR-0002), and it
branches every story off the configured `base`. A fresh superproject has neither:
`gh issue edit --add-label state:in-progress` fails with `'state:...' not found`,
and an iteration cannot branch off a `base` that does not exist. `ralph --init`
creates both, idempotently, so onboarding is one command instead of a pile of
manual `gh label create` calls.

The deterministic seam mirrors the completion stages: `init_plan` is a pure
function that returns the ordered git/gh commands (as argv lists) without running
anything, so the canonical label set and the base-branch policy are unit-testable.
`run_plan` executes a plan fail-fast; the CLI wrapper (`ralph --init`) detects the
live repo state (does `base` exist? what is the default branch?), builds the plan,
and prints + sets exit codes.

The label vocabulary is canonical and NOT configurable (ADR-0002): it is exactly
what `ralph_story`/`ralph_select` consume. `prio:N` is optional per story, so init
seeds a small starter range (prio:0..prio:5); authors add higher ones ad hoc.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402

PROTECTED_BRANCH = "main"
DEFAULT_PRIO_MAX = 5

# The canonical fixed vocabulary (name, color hex without '#', description).
# Mirrors ralph_story.STATES/TYPES/BLOCKER_LABEL and ralph_select.NEEDS_HUMAN.
FIXED_LABELS = [
    ("state:ready", "0e8a16", "Ready for Ralph to start"),
    ("state:in-progress", "fbca04", "Ralph is actively working this story"),
    ("state:awaiting-bench", "d93f0b",
     "HIL story: PR open, awaiting human bench verification"),
    ("state:blocked", "b60205",
     "Blocked: too many failed Attempts or an unmet dependency"),
    ("type:afk", "1d76db",
     "Away-from-keyboard: verifiable by CI alone; Ralph auto-merges when green"),
    ("type:hil", "5319e7",
     "Human-in-the-loop: needs physical bench verification"),
    ("needs-human", "e11d21",
     "Circuit breaker tripped: the loop is halted, awaiting a human"),
    ("ready-for-human", "0052cc",
     "Design-decision Blocker: kept out of state:ready until a human resolves it"),
]

PRIO_COLOR = "c5def5"


def prio_labels(prio_max=DEFAULT_PRIO_MAX):
    """The starter prio:0..prio:N labels (optional per story; lower = higher)."""
    return [("prio:%d" % n, PRIO_COLOR,
             "Priority %d (lower = higher priority; optional)" % n)
            for n in range(prio_max + 1)]


def canonical_labels(prio_max=DEFAULT_PRIO_MAX):
    return FIXED_LABELS + prio_labels(prio_max)


def _label_command(name, color, description):
    # --force makes it idempotent: create the label, or update its color and
    # description if it already exists. Safe to re-run on an initialized repo.
    return ["gh", "label", "create", name,
            "--color", color, "--description", description, "--force"]


def _base_commands(base, base_exists, default_branch):
    """Commands to ensure `base` exists on origin, or [] if nothing to do.

    Refuses to fabricate `main` (Ralph never touches main, ADR-0001) and never
    creates a base that already exists. When missing, `base` is branched off the
    repo's default branch on origin without disturbing the working tree.
    """
    if not base or base_exists:
        return []
    if base.strip().lower() == PROTECTED_BRANCH:
        return []
    return [
        ["git", "fetch", "origin", "--quiet"],
        ["git", "push", "origin",
         "origin/%s:refs/heads/%s" % (default_branch, base)],
    ]


def init_plan(base=None, base_exists=True, default_branch=PROTECTED_BRANCH,
              prio_max=DEFAULT_PRIO_MAX):
    """Build the ordered command plan to bootstrap a superproject.

    Pure: computes commands, runs nothing. Creates (or force-updates) the whole
    canonical label vocabulary, then, if `base` is missing, creates it off the
    default branch. Idempotent by construction.
    """
    commands = [_label_command(*lbl) for lbl in canonical_labels(prio_max)]
    commands += _base_commands(base, base_exists, default_branch)
    return commands


class CommandResult:
    def __init__(self, args, returncode, output):
        self.args = args
        self.returncode = returncode
        self.output = output
        self.ok = returncode == 0


class RunResult:
    def __init__(self, steps):
        self.steps = steps
        self.failed = next((s for s in steps if not s.ok), None)
        self.ok = self.failed is None


def run_plan(commands, cwd=None):
    """Execute plan commands (argv lists) in order, stopping at the first failure.

    Each command's combined stdout+stderr is captured (low-verbosity: the CLI
    surfaces output only for a failing command). Returns a RunResult.
    """
    results = []
    for args in commands:
        proc = subprocess.run(
            args, cwd=cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        results.append(CommandResult(args, proc.returncode, proc.stdout))
        if proc.returncode != 0:
            break
    return RunResult(results)


def _remote_has_branch(base, cwd=None):
    proc = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", base],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _default_branch(cwd=None):
    proc = subprocess.run(
        ["gh", "repo", "view", "--json", "defaultBranchRef",
         "--jq", ".defaultBranchRef.name"],
        cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    name = proc.stdout.strip() if proc.returncode == 0 else ""
    return name or PROTECTED_BRANCH


def _cmd_init(rest):
    config_path = rest[0] if rest and rest[0] else ".ralph.yml"

    result = ralph_config.load_and_validate(config_path)
    if not result.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        for err in result.errors:
            sys.stderr.write("  - %s\n" % err)
        sys.stderr.write("ralph --init needs a valid .ralph.yml (it reads the base branch)\n")
        return 2
    base = result.config["branching"]["base"]

    cwd = os.getcwd()
    base_exists = _remote_has_branch(base, cwd=cwd)
    default_branch = _default_branch(cwd=cwd)

    plan = init_plan(base=base, base_exists=base_exists,
                     default_branch=default_branch)
    run = run_plan(plan, cwd=cwd)
    if run.ok:
        n_labels = len(canonical_labels())
        if not base or base.strip().lower() == PROTECTED_BRANCH or base_exists:
            base_note = "base branch %r already present" % base
        else:
            base_note = "created base branch %r off %r" % (base, default_branch)
        print("OK: initialized %d labels; %s" % (n_labels, base_note))
        return 0
    sys.stderr.write("FAILED: init (exit %d): %s\n"
                     % (run.failed.returncode, " ".join(run.failed.args)))
    if run.failed.output.strip():
        sys.stderr.write(run.failed.output.rstrip() + "\n")
    return 1


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_init.py init [config]\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "init":
        return _cmd_init(rest)
    sys.stderr.write("ralph_init.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
