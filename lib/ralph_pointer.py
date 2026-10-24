"""The Superproject's ai-utils pointer versus the ai-utils that runs (#96).

A Superproject mounts ai-utils as a submodule and commits its gitlink: that
commit *is* the tooling version the repository says it runs. When the checkout
under it has moved on -- a local edit, a pull, a debugging session -- the Loop
runs one version while the repository records another, and the version being
debugged is not the one that ran (autopilot_controller pinned 8eba071 while
744b8b3 ran). The tick therefore compares the two before it does anything.

Pure `check(committed, running, allow_drift) -> PointerCheck`; `locate` and
`gitlink` read git. The comparison is against the gitlink in the Superproject's
committed tree (`HEAD`), never its index: a staged pointer is not committed.
Where ai-utils is not a submodule of the checkout -- it *is* the checkout root
(ADR-0001 amendment), or it is installed elsewhere -- there is nothing to
compare and the check passes.

Frozen tooling (ADR-0009) runs from a copy inside a git directory, not from the
mount, so the frozen tick names what it stands for: `RALPH_TOOLING_COMMIT` (the
running commit) and `RALPH_TOOLING_MOUNT` (the ai-utils path; empty when
self-hosted). The pin it is compared with is then the one the Tooling Commit was
resolved from, `origin/<base>`'s, falling back to `HEAD`'s.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config  # noqa: E402
import ralph_slot  # noqa: E402

OK = "ok"
NOT_MOUNTED = "not-mounted"
DRIFT_ALLOWED = "drift-allowed"
DRIFT = "drift"


class PointerCheck:
    def __init__(self, kind, message, committed=None, running=None):
        self.kind = kind
        self.message = message
        self.committed = committed
        self.running = running

    @property
    def ok(self):
        return self.kind != DRIFT


def check(committed, running, allow_drift=False, path="ai-utils"):
    if committed is None or running is None:
        return PointerCheck(NOT_MOUNTED, "ai-utils is not a submodule of this "
                            "checkout; there is no committed pointer to compare")
    if committed == running:
        return PointerCheck(OK, "ai-utils at %s matches the committed pointer"
                            % running[:12], committed, running)
    detail = ("the Superproject commits %s at %s, but ai-utils %s is running"
              % (path, committed, running))
    if allow_drift:
        return PointerCheck(DRIFT_ALLOWED, "running a mismatched ai-utils "
                            "(tooling.allow_pointer_drift): %s" % detail,
                            committed, running)
    return PointerCheck(DRIFT, "%s. Commit the pointer (git add %s && git commit) "
                        "so the version that runs is the version recorded, or set "
                        "tooling.allow_pointer_drift for deliberate local "
                        "development" % (detail, path), committed, running)


def _git(args, cwd):
    proc = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def locate(ralph_home, cwd):
    """(superproject root, ai-utils path inside it), or None when not nested.

    The root is the clone's main worktree, so the answer is the same from the
    operator's checkout and from a Slot (ADR-0008): a Tick runs in a Slot, which
    has no ai-utils under it, and the mount it must compare is the clone's.
    """
    root = ralph_slot.main_worktree(cwd)
    if not root:
        return None
    home = os.path.realpath(ralph_home)
    root = os.path.realpath(root)
    if home == root or not home.startswith(root + os.sep):
        return None
    return root, os.path.relpath(home, root)


def gitlink(root, path, treeish="HEAD"):
    """The commit the Superproject's *treeish* records for *path*, if a gitlink."""
    entry = _git(["ls-tree", treeish, "--", path], root)
    if not entry:
        return None
    mode, kind, sha = entry.split("\t", 1)[0].split()
    return sha if kind == "commit" else None


def main(argv):
    if not argv:
        sys.stderr.write("usage: ralph_pointer.py RALPH_HOME [CONFIG]\n")
        return 2
    ralph_home = argv[0]
    config_path = argv[1] if len(argv) > 1 and argv[1] else ".ralph.yml"
    validated = ralph_config.load_and_validate(config_path)
    if not validated.ok:
        sys.stderr.write("INVALID CONFIG: %s\n" % config_path)
        return 2
    allow = validated.config["tooling"]["allow_pointer_drift"]
    frozen = os.environ.get("RALPH_TOOLING_COMMIT")
    where = locate(ralph_home, os.getcwd())
    committed = running = None
    path = "ai-utils"
    if frozen:
        mount = os.environ.get("RALPH_TOOLING_MOUNT")
        root = ralph_slot.main_worktree(os.getcwd())
        if mount and root:
            path = mount
            base = validated.config["branching"]["base"]
            committed = (gitlink(root, path, "origin/%s" % base)
                         or gitlink(root, path))
            running = frozen if committed else None
    elif where is not None:
        root, path = where
        committed = gitlink(root, path)
        running = _git(["rev-parse", "HEAD"], ralph_home) if committed else None
    result = check(committed, running, allow, path)
    if not result.ok:
        sys.stderr.write("REFUSED: %s\n" % result.message)
        return 2
    print(("WARNING: %s" if result.kind == DRIFT_ALLOWED else "OK: %s") % result.message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
