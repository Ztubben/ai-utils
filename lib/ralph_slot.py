"""The Slot a Tick runs in (ADR-0008, PRD #133, #134).

A Tick used to run in the operator's own checkout. It switched branches there,
hard-synced to the remote and merged, so the checkout was not the operator's
while the Loop worked -- and anything they had in progress (a grilling session
editing `CONTEXT.md`, a half-finished merge, a conflicted `.ralph.yml`) either
got discarded or stopped the Tick outright.

Every Tick now runs in a **Slot**: a git worktree dedicated to one Loop, at a
conventional sibling path of the repository, `<repo>.ralph-slot-<N>`. The Tick
provisions it on first use and repairs it when it was deleted or is no longer a
worktree of this clone, so a scheduler keeps launching `ralph.sh` from the
target repository and nothing about its install changes. The Slot number comes
from the environment (the systemd template instance), defaulting to 1;
`RALPH_SLOT_DIR` overrides the path outright for a test or an odd layout.

The Tick lock is per Slot and lives in the **git common directory**, so every
worktree of one clone agrees on it and an overlapping Tick in the *same* Slot
still exits immediately. Slot 2 is not blocked by Slot 1.

A Slot is where a Tick does its git work. It is not where its tooling comes
from: which ai-utils runs is ADR-0009's decision, resolved against the clone's
main worktree so that it answers the same from the operator's checkout and from
any Slot (`main_worktree` is read by `ralph_tooling` and `ralph_pointer` for
exactly that).

`slot_number`, `slot_dir`, `lock_file` and `action` are pure; `resolve` and
`ensure` do the git work:

  ralph_slot.py resolve                 print "NUMBER LOCK_FILE SLOT_PATH"
  ralph_slot.py ensure PATH [COMMITISH] provision/repair; print created|repaired|reused
"""
import os
import re
import shutil
import subprocess
import sys

SLOT_ENV = "RALPH_SLOT"
SLOT_DIR_ENV = "RALPH_SLOT_DIR"
LOCK_DIR_ENV = "RALPH_LOCK_DIR"
DEFAULT_SLOT = "1"

# A Slot number reaches a filesystem path, so it is validated rather than
# quoted: no separator, no leading dash, nothing that could climb out.
SLOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
SLOT_SUFFIX = ".ralph-slot-"
LOCK_NAME = "ralph-tick-slot-%s.lock"

CREATED = "created"
REPAIRED = "repaired"
REUSED = "reused"


def slot_number(env):
    """The Slot this Tick runs in, or None when the environment names a bad one."""
    number = env.get(SLOT_ENV) or DEFAULT_SLOT
    return number if SLOT_RE.match(number) and ".." not in number else None


def slot_dir(main_worktree, number):
    """The conventional Slot path: a sibling of the repository, by number."""
    return main_worktree.rstrip(os.sep) + SLOT_SUFFIX + number


def lock_file(lock_dir, number):
    """The Tick lock for one Slot, in the git common directory."""
    return os.path.join(lock_dir, LOCK_NAME % number)


def action(exists, is_slot):
    """What provisioning has to do with the path the Slot should be at."""
    if not exists:
        return CREATED
    return REUSED if is_slot else REPAIRED


class Resolution:
    def __init__(self, number=None, lock=None, path=None, message="", ok=True):
        self.number = number
        self.lock = lock
        self.path = path
        self.message = message
        self.ok = ok


def resolve(cwd, env):
    """Which Slot this Tick runs in, where it is and which lock guards it."""
    number = slot_number(env)
    if number is None:
        return Resolution(ok=False, message="%s: %r is not a usable slot number "
                          "(letters, digits, '.', '_' and '-' only)"
                          % (SLOT_ENV, env.get(SLOT_ENV)))
    common = common_dir(cwd)
    main = main_worktree(cwd)
    if not common or not main:
        return Resolution(ok=False, message="%s is not a git repository, so it "
                          "has no slot" % cwd)
    path = env.get(SLOT_DIR_ENV) or slot_dir(main, number)
    return Resolution(number, lock_file(env.get(LOCK_DIR_ENV) or common, number),
                      os.path.abspath(path))


def ensure(path, repo, commitish=None):
    """(path, action) of *repo*'s Slot at *path*, provisioning or repairing it;
    (None, error) when it cannot be made."""
    common = common_dir(repo)
    main = main_worktree(repo)
    if not common or not main:
        return None, "%s is not a git repository" % repo
    real = os.path.realpath(path)
    # A Slot is never the operator's checkout, whatever the environment says.
    if real in (os.path.realpath(main), os.path.realpath(repo)):
        return None, "%s is the checkout itself, not a slot" % path
    exists = os.path.isdir(path)
    what = action(exists, exists and _is_worktree_of(path, common))
    if what == REUSED:
        return path, REUSED
    if exists and not _remove(repo, path):
        return None, "cannot remove %s to repair the slot" % path
    # A Slot whose directory was deleted is still registered; `worktree add`
    # would refuse the path until the stale admin entry is pruned.
    _git(["worktree", "prune"], repo)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError as exc:
        return None, "cannot create %s: %s" % (os.path.dirname(path), exc)
    args = ["worktree", "add", "-q", "--detach", path]
    if commitish:
        args.append(commitish)
    proc = subprocess.run(["git"] + args, cwd=repo, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        return None, "cannot create the slot at %s: %s" % (path, proc.stderr.strip())
    return path, what


# --- git -------------------------------------------------------------------

def _git(args, cwd):
    proc = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def common_dir(cwd):
    """The git common directory of the clone *cwd* belongs to, absolute."""
    found = _git(["rev-parse", "--git-common-dir"], cwd)
    if not found:
        return None
    return os.path.normpath(os.path.join(cwd, found))


def main_worktree(cwd):
    """The clone's main worktree: the same answer from the operator's checkout,
    from a Slot and from a frozen tooling copy, which is what makes it the one
    thing a Tick may resolve its tooling and its Slot against."""
    listing = _git(["worktree", "list", "--porcelain"], cwd)
    if not listing:
        return None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):]
    return None


def _is_worktree_of(path, common):
    found = common_dir(path)
    return bool(found) and os.path.realpath(found) == os.path.realpath(common)


def _remove(repo, path):
    subprocess.run(["git", "worktree", "remove", "--force", "--force", path],
                   cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    return not os.path.exists(path)


# --- CLI -------------------------------------------------------------------

def cmd_resolve(argv):
    result = resolve(os.getcwd(), os.environ)
    if not result.ok:
        sys.stderr.write("REFUSED: %s\n" % result.message)
        return 2
    print("%s %s %s" % (result.number, result.lock, result.path))
    return 0


def cmd_ensure(argv):
    if not argv:
        sys.stderr.write("usage: ralph_slot.py ensure PATH [COMMITISH]\n")
        return 2
    path, detail = ensure(argv[0], os.getcwd(), argv[1] if len(argv) > 1 else None)
    if path is None:
        sys.stderr.write("REFUSED: %s\n" % detail)
        return 2
    print(detail)
    return 0


def main(argv):
    commands = {"resolve": cmd_resolve, "ensure": cmd_ensure}
    if not argv or argv[0] not in commands:
        sys.stderr.write("usage: ralph_slot.py {%s} ...\n" % ",".join(sorted(commands)))
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
