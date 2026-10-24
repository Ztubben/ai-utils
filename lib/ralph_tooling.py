"""The Tooling Commit a Tick runs, and its frozen copy (ADR-0009, #146).

A Tick used to run the tooling it found next to itself: in a Superproject the
mounted submodule checkout, which can move while the Tick works; when ai-utils
is its own target repository, the very checkout the Implementation Agent edits,
so a Story changing `bin/ralph.sh` or `lib/` ran its own half-written code. Now
the Tick decides once, at its start, which commit of ai-utils runs -- the
**Tooling Commit** -- and runs it from a detached worktree of that commit that
is created once and never modified.

  * self-hosting (ai-utils is the checkout root): `origin/<base>` after a fetch,
    so the Loop upgrades itself at most once per Tick;
  * Superproject: the gitlink at the ai-utils path in `origin/<base>`'s tree,
    never the index or the local submodule checkout, so upgrading stays a pin
    bump. `tooling.allow_pointer_drift` opts out: the mounted checkout runs
    unfrozen, as before, and the pointer check warns;
  * ai-utils installed outside the checkout: nothing to freeze, it runs as-is.

Copies that are no longer needed are pruned (#147): every copy that is neither
the current Tooling Commit nor Last-Known-Good, and that no running Tick uses.
A Tick holds its copy's `<commit>.lock` (beside the copy) shared for as long as
it runs from it; pruning takes that lock exclusively and keeps it while it
removes the copy, so a Tick about to use the copy waits and then recreates it.
The lock files stay: removing one would let a later Tick lock a fresh file
while an earlier one still holds the old.

`where`, `resolve` and `prune_plan` are pure (git is read through an injected
function); `copy_path` names a copy. The CLI does the git work:

  ralph_tooling.py commit [CONFIG]      print "KIND COMMIT REPO MOUNT COPIES", or
                                        "unfrozen" with the reason on stderr
  ralph_tooling.py copy COMMIT REPO     make sure COMMIT's copy exists; print it
  ralph_tooling.py prune COPIES KEEP... remove the copies nothing needs
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402

RALPH_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SELF_HOSTED = "self-hosted"
SUPERPROJECT = "superproject"
ELSEWHERE = "elsewhere"
UNFROZEN = "unfrozen"

# Where the copies live, inside the git common directory of the repository that
# owns ai-utils: one detached worktree per Tooling Commit, named by the commit.
COPIES_DIR = "ralph-tooling"
LAST_KNOWN_GOOD = "last-known-good"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class Resolution:
    def __init__(self, kind, commit=None, repo=None, mount=None, message="",
                 ok=True):
        self.kind = kind
        self.commit = commit
        self.repo = repo        # the repository whose worktree the copy is
        self.mount = mount      # the ai-utils path inside a Superproject
        self.message = message
        self.ok = ok

    @property
    def frozen(self):
        return self.ok and self.kind in (SELF_HOSTED, SUPERPROJECT)


def where(ralph_home, toplevel):
    """(kind, mount): how the running ai-utils sits in the checkout at *toplevel*."""
    if not toplevel:
        return ELSEWHERE, None
    home = os.path.realpath(ralph_home)
    root = os.path.realpath(toplevel)
    if home == root:
        return SELF_HOSTED, None
    if home.startswith(root + os.sep):
        return SUPERPROJECT, os.path.relpath(home, root)
    return ELSEWHERE, None


def resolve(ralph_home, toplevel, base, allow_drift, read):
    """Decide the Tooling Commit.

    *read(kind, *args)* answers the git questions: ("rev", ref) for a commit id,
    ("gitlink", treeish, path) for the commit a tree records at a path. Either
    returns None when git cannot answer.
    """
    kind, mount = where(ralph_home, toplevel)
    if kind == ELSEWHERE:
        return Resolution(UNFROZEN, message="ai-utils at %s is not part of this "
                          "checkout; running it as installed" % ralph_home)
    if kind == SELF_HOSTED:
        commit = read("rev", "origin/%s" % base)
        if not commit:
            return Resolution(SELF_HOSTED, ok=False, message="cannot resolve the "
                              "Tooling Commit: origin/%s does not name a commit"
                              % base)
        return Resolution(SELF_HOSTED, commit, repo=toplevel,
                          message="ai-utils is the target repository; the "
                          "Tooling Commit is origin/%s" % base)
    if allow_drift:
        return Resolution(UNFROZEN, mount=mount, message="tooling.allow_pointer_"
                          "drift: running the mounted %s checkout unfrozen" % mount)
    commit = read("gitlink", "origin/%s" % base, mount)
    if not commit:
        return Resolution(SUPERPROJECT, mount=mount, ok=False, message="cannot "
                          "resolve the Tooling Commit: origin/%s records no "
                          "gitlink at %s" % (base, mount))
    return Resolution(SUPERPROJECT, commit, repo=os.path.join(toplevel, mount),
                      mount=mount, message="the Tooling Commit is the %s pinned "
                      "in origin/%s" % (mount, base))


def copies_dir(common_dir):
    return os.path.join(common_dir, COPIES_DIR)


def copy_path(common_dir, commit):
    return os.path.join(copies_dir(common_dir), commit)


def lock_path(copies, commit):
    return os.path.join(copies, commit + ".lock")


def prune_plan(copies, keep, last_known_good, in_use):
    """The copies to remove: not kept (the current Tooling Commit), not
    Last-Known-Good, and not in use by a running Tick."""
    spared = set(keep) | set(in_use) | ({last_known_good} if last_known_good else set())
    return sorted(c for c in copies if c not in spared)


def read_last_known_good(copies):
    try:
        with open(os.path.join(copies, LAST_KNOWN_GOOD)) as fh:
            commit = fh.read().strip()
    except OSError:
        return None
    return commit if COMMIT_RE.match(commit) else None


def list_copies(copies):
    try:
        names = os.listdir(copies)
    except OSError:
        return []
    return sorted(n for n in names
                  if COMMIT_RE.match(n) and os.path.isdir(os.path.join(copies, n)))


def prune(copies, keep, log):
    """Remove every copy `prune_plan` names; best-effort. Returns the removed."""
    lkg = read_last_known_good(copies)
    # git is run from a copy that stays, a worktree of the same repository.
    repo = next((os.path.join(copies, k) for k in keep
                 if os.path.isdir(os.path.join(copies, k))), copies)
    held, in_use = [], []
    candidates = prune_plan(list_copies(copies), keep, lkg, ())
    try:
        # Every candidate's lock is taken before anything is decided, and held
        # until it is removed: a Tick that wants it back waits, then recreates.
        for commit in candidates:
            try:
                fd = os.open(lock_path(copies, commit), os.O_WRONLY | os.O_CREAT, 0o644)
            except OSError as exc:
                log("cannot lock the frozen copy of %s (%s); leaving it" % (commit, exc))
                in_use.append(commit)
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                log("the frozen copy of %s is in use by a running tick; leaving it" % commit)
                in_use.append(commit)
                continue
            held.append(fd)
        removed = []
        for commit in prune_plan(candidates, keep, lkg, in_use):
            path = os.path.join(copies, commit)
            if _remove(repo, path):
                log("pruned the frozen copy of %s" % commit)
                removed.append(commit)
            else:
                log("could not remove the frozen copy of %s at %s; a later tick retries"
                    % (commit, path))
        return removed
    finally:
        for fd in held:
            os.close(fd)


# --- git -------------------------------------------------------------------

def _git(args, cwd):
    proc = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _reader(toplevel):
    def read(kind, *args):
        if kind == "rev":
            return _git(["rev-parse", "-q", "--verify", args[0] + "^{commit}"],
                        toplevel)
        if kind == "gitlink":
            entry = _git(["ls-tree", args[0], "--", args[1]], toplevel)
            if not entry:
                return None
            _, otype, sha = entry.split("\t", 1)[0].split()
            return sha if otype == "commit" else None
        raise ValueError(kind)
    return read


def common_dir(repo):
    found = _git(["rev-parse", "--git-common-dir"], repo)
    if not found:
        return None
    return os.path.normpath(os.path.join(repo, found))


def ensure_copy(commit, repo):
    """(path, created) of *commit*'s copy, creating it once; (None, error)."""
    common = common_dir(repo)
    if not common:
        return None, "%s is not a git repository" % repo
    if _git(["cat-file", "-e", commit + "^{commit}"], repo) is None:
        # A Superproject pin the submodule's repository has not fetched yet.
        _git(["fetch", "-q", "origin"], repo)
        if _git(["cat-file", "-e", commit + "^{commit}"], repo) is None:
            return None, "%s does not have commit %s" % (repo, commit)
    path = copy_path(common, commit)
    if os.path.isdir(path):
        if _git(["rev-parse", "HEAD"], path) == commit:
            return path, False
        # A half-made copy (a Tick killed mid-creation): never reused.
        _remove(repo, path)
    os.makedirs(copies_dir(common), exist_ok=True)
    proc = subprocess.run(["git", "worktree", "add", "-q", "--detach", path, commit],
                          cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)
    if proc.returncode != 0:
        return None, "cannot create the copy of %s: %s" % (commit, proc.stderr.strip())
    return path, True


def _remove(repo, path):
    """Remove the copy at *path*; *repo* is any worktree of its repository."""
    subprocess.run(["git", "worktree", "remove", "--force", "--force", path],
                   cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(path):
        # The worktree's admin entry in the git directory goes with it.
        subprocess.run(["git", "worktree", "prune"], cwd=repo,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return not os.path.exists(path)


# --- CLI -------------------------------------------------------------------

def cmd_commit(argv):
    config_path = argv[0] if argv else ".ralph.yml"
    base, allow = "main", False
    validated = ralph_config.load_and_validate(config_path)
    if validated.ok:
        base = validated.config["branching"]["base"]
        allow = validated.config["tooling"]["allow_pointer_drift"]
    toplevel = _git(["rev-parse", "--show-toplevel"], os.getcwd())
    kind, _ = where(RALPH_HOME, toplevel)
    if kind != ELSEWHERE and _git(["fetch", "-q", "origin"], toplevel) is None:
        sys.stderr.write("ralph: could not fetch origin; resolving the Tooling "
                         "Commit from the local refs\n")
    result = resolve(RALPH_HOME, toplevel, base, allow, _reader(toplevel))
    if not result.ok:
        sys.stderr.write("REFUSED: %s\n" % result.message)
        return 2
    if not result.frozen:
        sys.stderr.write("ralph: %s\n" % result.message)
        print(UNFROZEN)
        return 0
    common = common_dir(result.repo)
    if not common:
        sys.stderr.write("REFUSED: %s is not a git repository\n" % result.repo)
        return 2
    print("%s %s %s %s %s" % (result.kind, result.commit, result.repo,
                              result.mount or "-", copies_dir(common)))
    return 0


def cmd_copy(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: ralph_tooling.py copy COMMIT REPO\n")
        return 2
    path, detail = ensure_copy(argv[0], argv[1])
    if path is None:
        sys.stderr.write("REFUSED: %s\n" % detail)
        return 2
    sys.stderr.write("ralph: %s the frozen copy of %s at %s\n"
                     % ("created" if detail else "reusing", argv[0], path))
    print(path)
    return 0


def cmd_prune(argv):
    if len(argv) < 2:
        sys.stderr.write("usage: ralph_tooling.py prune COPIES KEEP...\n")
        return 2
    prune(argv[0], argv[1:], lambda line: print("ralph: %s" % line))
    return 0


def main(argv):
    commands = {"commit": cmd_commit, "copy": cmd_copy, "prune": cmd_prune}
    if not argv or argv[0] not in commands:
        sys.stderr.write("usage: ralph_tooling.py {%s} ...\n" % ",".join(sorted(commands)))
        return 2
    return commands[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
