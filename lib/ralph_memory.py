"""Two-tier memory for the Ralph Loop (US-010, ADR-0005).

Ralph keeps cross-iteration memory in exactly two stores, split by durability:

  - durable **Learnings** in nested `AGENTS.md` files, committed in the
    superproject, read at story start and promoted to the nearest module-local
    `AGENTS.md` before completion (never one growing global brain-dump); and
  - transient, story-specific notes on the issue (the **Handoff**, US-008).

There is **no** `progress.txt` -- its audit-log job is covered by git history,
issue comments, and PRs (ADR-0005).

The deterministic seams are pure filesystem queries:
  - `nested_agents_md(start_dir, root)` -- the AGENTS.md Ralph reads at story
    start, nearest-first, from `start_dir` up to and including `root`.
  - `promotion_target(changed_path, root)` -- the nearest AGENTS.md a reusable
    learning is promoted to, kept module-local rather than dumped at the root.
  - `is_progress_txt` / `find_progress_txt` -- guard that Ralph neither reads nor
    creates a progress.txt.

The judgment-heavy memory discipline lives in the checked-in prompt
`prompts/memory.v1.md`.
"""
import os
import sys

AGENTS_MD = "AGENTS.md"
PROGRESS_TXT = "progress.txt"


def _iter_dirs(start, root):
    """Yield directories from `start` up to and including `root`, nearest-first.

    Stops at `root` (inclusive) or at the filesystem root, whichever comes first,
    so a `root` that is not an ancestor of `start` can never loop forever.
    """
    cur = os.path.abspath(start)
    root = os.path.abspath(root)
    while True:
        yield cur
        if cur == root:
            return
        parent = os.path.dirname(cur)
        if parent == cur:
            return
        cur = parent


def nested_agents_md(start_dir, root):
    """The AGENTS.md files Ralph reads at the start of a story (AC#1).

    Walks from `start_dir` up to and including `root`, collecting each directory's
    `AGENTS.md` where present, nearest-first. Never returns a progress.txt.
    """
    out = []
    for d in _iter_dirs(start_dir, root):
        cand = os.path.join(d, AGENTS_MD)
        if os.path.isfile(cand):
            out.append(cand)
    return out


def promotion_target(changed_path, root):
    """The nearest AGENTS.md a reusable learning should be promoted to (AC#2/#3).

    Starts at `changed_path`'s directory (or `changed_path` itself when it is a
    directory) and returns the first existing `AGENTS.md` walking up to `root`.
    When none exists anywhere in that chain, the learning is kept **module-local**
    by targeting a new `AGENTS.md` in the changed file's own directory rather than
    the global root. The result is always an AGENTS.md, never a progress.txt.
    """
    start = changed_path if os.path.isdir(changed_path) else os.path.dirname(changed_path)
    for d in _iter_dirs(start, root):
        cand = os.path.join(d, AGENTS_MD)
        if os.path.isfile(cand):
            return cand
    return os.path.join(os.path.abspath(start), AGENTS_MD)


def is_progress_txt(path):
    """True if `path` names a progress.txt (ADR-0005: Ralph uses none)."""
    return os.path.basename(path) == PROGRESS_TXT


def find_progress_txt(root):
    """Every progress.txt under `root` (empty for a clean tree). AC#5 guard."""
    hits = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name == PROGRESS_TXT:
                hits.append(os.path.join(dirpath, name))
    return sorted(hits)


def _cmd_read_learnings(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --read-learnings requires a DIR\n")
        return 2
    start = rest[0]
    if not os.path.isdir(start):
        sys.stderr.write("ralph: not a directory: %s\n" % start)
        return 2
    root = rest[1] if len(rest) > 1 and rest[1] else start
    for path in nested_agents_md(start, root):
        print(path)
    return 0


def _cmd_learn_target(rest):
    if not rest or not rest[0]:
        sys.stderr.write("ralph: --learn-target requires a PATH\n")
        return 2
    changed = rest[0]
    root = rest[1] if len(rest) > 1 and rest[1] else os.getcwd()
    print(promotion_target(changed, root))
    return 0


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage: ralph_memory.py {read-learnings <dir> [root] | "
            "learn-target <path> [root]}\n")
        return 2
    mode, rest = argv[0], argv[1:]
    if mode == "read-learnings":
        return _cmd_read_learnings(rest)
    if mode == "learn-target":
        return _cmd_learn_target(rest)
    sys.stderr.write("ralph_memory.py: unknown mode: %s\n" % mode)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
