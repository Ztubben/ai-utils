"""Story Claims on origin (ADR-0008, #136).

Lease encoding and command plans are pure. The CLI below reads the remote,
creates an empty-tree commit, and executes a plan; no branch or checkout is
used to represent a Claim. Renewal and expiry takeover are owned by #137.
"""
from dataclasses import dataclass, field
import json
import math
import os
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ralph_config
import ralph_slot

PREFIX = "refs/ralph/claims/"
OID_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


@dataclass(frozen=True)
class Lease:
    holder: str
    expiry: float


@dataclass
class Plan:
    ok: bool = True
    errors: list = field(default_factory=list)
    commands: list = field(default_factory=list)


def decode(value):
    """Read a lease from a commit message; reject malformed coordination data."""
    try:
        data = json.loads(value)
        holder, expiry = data["holder"], data["expiry"]
        if (not isinstance(holder, str) or not holder.strip()
                or ":" not in holder or not all(holder.rsplit(":", 1))
                or isinstance(expiry, bool) or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry) or expiry <= 0):
            raise ValueError("invalid holder or expiry")
        return Lease(holder, expiry)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("claim lease: expected holder <host>:<slot> and numeric expiry") from exc


def encode(lease):
    value = json.dumps({"holder": lease.holder, "expiry": lease.expiry}, sort_keys=True)
    decode(value)
    return value + "\n"


def claim_ref(issue):
    if isinstance(issue, bool) or not re.fullmatch(r"[1-9][0-9]*", str(issue)):
        raise ValueError("claim/issue: expected a positive issue number")
    return PREFIX + str(issue)


def _ref_plan(issue):
    try:
        return claim_ref(issue), None
    except ValueError as exc:
        return None, Plan(False, [str(exc)])


def _push(issue, oid, expected):
    ref, error = _ref_plan(issue)
    if error:
        return error
    if (oid and not OID_RE.fullmatch(oid)) or (expected and not OID_RE.fullmatch(expected)):
        return Plan(False, ["claim/oid: expected a full git object id"])
    return Plan(commands=[["git", "push", "--porcelain",
                           "--force-with-lease=%s:%s" % (ref, expected),
                           "origin", "%s:%s" % (oid, ref)]])


def claim_plan(issue, oid, observed=None):
    """Create a Claim only if absent; an existing Claim always names its holder."""
    if observed is not None:
        return Plan(False, ["claim #%s held by %s" % (issue, observed.holder)])
    if not oid:
        return Plan(False, ["claim/oid: expected a full git object id"])
    return _push(issue, oid, "")


def release_plan(issue, last_seen):
    """Delete exactly the Claim the caller acquired, never a replacement."""
    if not last_seen:
        return Plan(False, ["claim/last_seen: a release requires the last-seen object id"])
    return _push(issue, "", last_seen)


def verify_plan(issue):
    ref, error = _ref_plan(issue)
    return error or Plan(commands=[["git", "ls-remote", "origin", ref]])


# --- CLI I/O: the plans above never execute commands -----------------------

def _git(args, cwd, input=None):
    # Claim commits need an identity even on a machine with no git user config.
    env = dict(os.environ)
    for role in ("AUTHOR", "COMMITTER"):
        env.setdefault("GIT_%s_NAME" % role, "Ralph Claim")
        env.setdefault("GIT_%s_EMAIL" % role, "ralph@localhost")
    proc = subprocess.run(["git"] + args, cwd=cwd, input=input, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode:
        raise ValueError("git %s: %s" % (args[0], proc.stderr.strip()))
    return proc.stdout.strip()


def run_plan(plan, cwd):
    if not plan.ok:
        raise ValueError("; ".join(plan.errors))
    return [_git(command[1:], cwd) for command in plan.commands]


def _listing(value):
    refs = {}
    for line in value.splitlines():
        oid, ref = line.split()
        if not OID_RE.fullmatch(oid):
            raise ValueError("claim/oid: invalid remote object id")
        if ref.startswith(PREFIX):
            claim_ref(ref[len(PREFIX):])
            refs[int(ref[len(PREFIX):])] = oid
    return refs


def _lease(oid, cwd):
    # Fetch the observed object, not a shared local tracking ref: worktrees
    # share refs and two readers must not overwrite each other's observations.
    _git(["fetch", "--no-tags", "--no-write-fetch-head", "origin", oid], cwd)
    return decode(_git(["show", "-s", "--format=%B", oid], cwd))


def observe(issue, cwd):
    listing = run_plan(verify_plan(issue), cwd)[0]
    oid = _listing(listing).get(int(issue))
    return (oid, _lease(oid, cwd)) if oid else (None, None)


def holder(env):
    slot = ralph_slot.slot_number(env)
    if slot is None:
        raise ValueError("RALPH_SLOT: invalid Slot number")
    return socket.gethostname() + ":" + slot


def _refusal(issue, cwd, detail):
    try:
        _, lease = observe(issue, cwd)
        if lease:
            return "claim #%s held by %s (%s)" % (issue, lease.holder, detail)
    except (ValueError, OSError):
        pass
    return "claim #%s: %s" % (issue, detail)


def main(argv):
    if not argv or argv[0] not in ("claim", "release", "verify", "list"):
        sys.stderr.write("usage: ralph_claim.py {claim ISSUE [CONFIG]|release ISSUE OID|verify ISSUE|list}\n")
        return 2
    command, args = argv[0], argv[1:]
    cwd = os.getcwd()
    issue = None
    try:
        who = holder(os.environ)
        if command == "list":
            if args:
                raise ValueError("claimed-issues takes no arguments")
            refs = _listing(_git(["ls-remote", "origin", PREFIX + "*"], cwd))
            for number, oid in sorted(refs.items()):
                if _lease(oid, cwd).holder != who:
                    print(number)
            return 0
        if not args:
            raise ValueError("claim/issue: an issue number is required")
        issue = args[0]
        claim_ref(issue)
        if command == "release":
            if len(args) != 2:
                raise ValueError("release-claim requires ISSUE and last-seen OID")
            run_plan(release_plan(issue, args[1]), cwd)
            return 0
        if command == "verify" and len(args) != 1:
            raise ValueError("verify-claim requires ISSUE")
        if command == "claim" and len(args) > 2:
            raise ValueError("claim requires ISSUE [CONFIG]")
        _, lease = observe(issue, cwd)
        if command == "verify":
            if lease is None:
                raise ValueError("no Claim exists")
            if lease.holder != who:
                raise ValueError("caller %s does not hold this Claim" % who)
            print(lease.holder)
            return 0
        config = ralph_config.load_and_validate(args[1] if len(args) > 1 else ".ralph.yml")
        if not config.ok:
            raise ValueError("; ".join(config.errors))
        if lease:
            raise ValueError("; ".join(claim_plan(issue, "", lease).errors))
        value = encode(Lease(who, time.time() + config.config["concurrency"]["claim_ttl"]))
        tree = _git(["mktree"], cwd, input="")
        oid = _git(["commit-tree", tree], cwd, input=value)
        run_plan(claim_plan(issue, oid), cwd)
        print(oid)
        return 0
    except (ValueError, OSError) as exc:
        detail = _refusal(issue, cwd, str(exc)) if issue else str(exc)
        sys.stderr.write("REFUSED: %s\n" % detail)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
