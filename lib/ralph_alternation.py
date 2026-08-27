"""Role alternation across newly started Stories (#47, PRD #42).

Resolution (#44) picks two Model Profiles and assignment (#46) records them on
the Story. Alternation is what keeps that choice *balanced*: Ralph treats the
two profiles as a **pair** and swaps which one implements and which one reviews
every time a Story that carries no assignment starts. The resolved role order --
the committed defaults, or the operator's `--implementation` / `--review` order
-- is the first newly assigned Story's order; the next newly assigned Story runs
the same pair the other way round.

Alternation advances on a **fresh pair only**. A resumed checkpointed Story, a
retried failed Attempt, and a further review round all read their roles off the
Story's own labels, so they never reach the phase: no Story swaps models midway,
and a resume cannot consume the swap the next newly started Story is owed. A
half-assigned Story (a crash between the two label writes) heals forward for the
same reason -- it already started, so it is not a fresh pair.

The phase is one integer -- the number of pairs assigned so far -- and the order
is its parity. It is durable but **loop-local**: it lives under the target
repository's git dir, next to the tick lock, so it survives across ticks without
entering the working tree or the backlog. It is a balance heuristic, not a
correctness invariant, so a missing or damaged state file starts the alternation
over rather than failing a tick, and a checkout with no git dir simply never
alternates (`state_path` is None) instead of refusing to assign.

`ralph_models` owns *which* roles are assigned; this module owns only the
ordering and its state, and imports nothing from the rest of the loop.
"""
import json
import os

# One integer under the git dir: `{"version": 1, "phase": N}`.
STATE_VERSION = 1
STATE_DIR_NAME = "ralph"
STATE_FILE_NAME = "alternation"

# Config key: alternation is on unless a target repository turns it off.
CONFIG_KEY = "alternate"


def enabled(config):
    """True unless the target repository committed the fixed-role option."""
    value = (config.get("models") or {}).get(CONFIG_KEY)
    return True if value is None else bool(value)


def swaps(phase):
    """True when `phase` runs the pair the other way round."""
    return phase % 2 == 1


def order_for(phase, implementation, review):
    """The (implementation, review) order this phase assigns."""
    return (review, implementation) if swaps(phase) else (implementation, review)


def advanced(phase):
    """The phase the next newly assigned Story gets."""
    return phase + 1


def git_dir(root=None):
    """The target repository's git dir, or None when there is no checkout.

    Follows the `gitdir:` pointer a submodule or worktree checkout leaves in
    place of a `.git` directory, so the state lands where the repository really
    keeps its metadata.
    """
    root = os.path.abspath(root or os.getcwd())
    candidate = os.path.join(root, ".git")
    if os.path.isdir(candidate):
        return candidate
    if os.path.isfile(candidate):
        try:
            with open(candidate) as fh:
                first = fh.readline().strip()
        except OSError:
            return None
        if first.startswith("gitdir:"):
            pointed = first[len("gitdir:"):].strip()
            if pointed:
                return os.path.join(root, pointed) if not os.path.isabs(pointed) \
                    else pointed
    return None


def state_path(root=None):
    """Where the alternation phase is recorded, or None when nowhere durable."""
    gd = git_dir(root)
    if gd is None:
        return None
    return os.path.join(gd, STATE_DIR_NAME, STATE_FILE_NAME)


def read_phase(path):
    """The recorded phase; 0 when it is missing, unreadable, or damaged.

    Starting the alternation over costs at most one repeated role order, which
    is cheaper than failing a tick over a balance heuristic.
    """
    if not path:
        return 0
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return 0
    phase = data.get("phase") if isinstance(data, dict) else None
    if not isinstance(phase, int) or isinstance(phase, bool) or phase < 0:
        return 0
    return phase


def write_phase(path, phase):
    """Record `phase`; returns False when it could not be recorded.

    Best-effort, like the rest of the tick's bookkeeping: a state dir that
    cannot be written means the next Story repeats an order, not that the
    assignment is rolled back.
    """
    if not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"version": STATE_VERSION, "phase": phase}, fh)
            fh.write("\n")
    except OSError:
        return False
    return True
