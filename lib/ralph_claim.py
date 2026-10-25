"""Story Claims on origin (ADR-0008, #136, #137).

Lease encoding, expiry and command plans are pure. The CLI below reads the
remote, creates an empty-tree commit, and executes a plan; no branch or
checkout is used to represent a Claim. A Claim is a *lease*: it is renewed by
a heartbeat while its unit of work runs, and once its expiry has passed any
Slot may take it over, so a crashed or suspended machine releases its issue
within one lease.
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
# A lease is renewed at a fifth of its length (ADR-0008: ten minutes, every
# two), so four renewals may be lost before a Claim lapses.
RENEWALS_PER_LEASE = 5
# How often the heartbeat looks up from its sleep to see whether the Tick that
# started it is still there. A heartbeat must never outlive its Tick.
POLL = 0.05


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


def expired(lease, now):
    """A lease is over the instant its expiry is reached, not a moment later."""
    return lease.expiry <= now


def heartbeat_interval(ttl):
    """How often a held Claim is renewed, derived from `concurrency.claim_ttl`."""
    if (isinstance(ttl, bool) or not isinstance(ttl, (int, float))
            or not math.isfinite(ttl) or ttl <= 0):
        raise ValueError("concurrency/claim_ttl: expected a positive number of seconds")
    return ttl / RENEWALS_PER_LEASE


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


def claim_plan(issue, oid, observed=None, observed_oid=None, now=None):
    """Create a Claim only if absent, or take over one whose lease has expired;
    a Claim still held always names its holder. A takeover swaps from the exact
    value observed, so a holder that renewed in the meantime keeps its Claim."""
    if observed is not None:
        if not expired(observed, time.time() if now is None else now):
            return Plan(False, ["claim #%s held by %s" % (issue, observed.holder)])
        if not observed_oid:
            return Plan(False, ["claim/observed: a takeover requires the "
                                "object id the expired Claim was seen at"])
    if not oid:
        return Plan(False, ["claim/oid: expected a full git object id"])
    return _push(issue, oid, observed_oid or "")


def renew_plan(issue, oid, last_seen):
    """Advance the expiry by compare-and-swap from the value last written, so a
    holder that has lost the Claim cannot overwrite its new holder's lease."""
    if not last_seen:
        return Plan(False, ["claim/last_seen: a renewal requires the last-seen object id"])
    if not oid:
        return Plan(False, ["claim/oid: expected a full git object id"])
    return _push(issue, oid, last_seen)


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


def write_lease(who, ttl, cwd):
    """A Claim's value: an empty-tree commit whose message is the lease."""
    value = encode(Lease(who, time.time() + ttl))
    tree = _git(["mktree"], cwd, input="")
    return _git(["commit-tree", tree], cwd, input=value)


def renew(issue, who, ttl, last_seen, cwd):
    """One heartbeat: a fresh expiry swapped in for the value last written."""
    oid = write_lease(who, ttl, cwd)
    run_plan(renew_plan(issue, oid, last_seen), cwd)
    return oid


def _tick_alive(interval, tick):
    """Sleep out one interval, unless the Tick that started us goes away."""
    deadline = time.time() + interval
    while True:
        if os.getppid() != tick:
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(POLL, remaining))


def heartbeat(issue, who, ttl, oid, cwd, out):
    """Keep a Claim alive for as long as its unit of work runs, and no longer.
    The heartbeat is a child of the Tick and watches it: a Tick killed outright
    runs no trap, and an orphan still renewing would lock its issue away from
    every other Slot for as long as the machine stays up. Each oid swapped to
    is reported, because releasing the Claim must name the last one."""
    interval, tick = heartbeat_interval(ttl), os.getppid()
    while _tick_alive(interval, tick):
        oid = renew(issue, who, ttl, oid, cwd)
        out.write(oid + "\n")
        out.flush()


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


COMMANDS = ("claim", "renew", "heartbeat", "release", "verify", "list")
USAGE = ("usage: ralph_claim.py {claim ISSUE [CONFIG]|renew ISSUE OID [CONFIG]"
         "|heartbeat ISSUE OID [CONFIG]|release ISSUE OID|verify ISSUE|list}\n")


def _ttl(path):
    config = ralph_config.load_and_validate(path or ".ralph.yml")
    if not config.ok:
        raise ValueError("; ".join(config.errors))
    return config.config["concurrency"]["claim_ttl"]


def main(argv):
    if not argv or argv[0] not in COMMANDS:
        sys.stderr.write(USAGE)
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
            now = time.time()
            for number, oid in sorted(refs.items()):
                # An expired Claim holds nothing back: whoever selects the
                # issue next takes it over, so it is not another Slot's.
                lease = _lease(oid, cwd)
                if lease.holder != who and not expired(lease, now):
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
        if command == "verify":
            if len(args) != 1:
                raise ValueError("verify-claim requires ISSUE")
            _, lease = observe(issue, cwd)
            if lease is None:
                raise ValueError("no Claim exists")
            if lease.holder != who:
                raise ValueError("caller %s does not hold this Claim" % who)
            print(lease.holder)
            return 0
        if command == "claim":
            if len(args) > 2:
                raise ValueError("claim requires ISSUE [CONFIG]")
            ttl = _ttl(args[1] if len(args) > 1 else None)
            observed_oid, lease = observe(issue, cwd)
            now = time.time()
            if lease and not expired(lease, now):
                raise ValueError("; ".join(
                    claim_plan(issue, "", lease, observed_oid, now).errors))
            oid = write_lease(who, ttl, cwd)
            run_plan(claim_plan(issue, oid, lease, observed_oid, now), cwd)
            print(oid)
            return 0
        # renew and heartbeat swap from the value the caller last wrote, so
        # neither reads the remote: the compare-and-swap is the whole check.
        if not 2 <= len(args) <= 3:
            raise ValueError("%s-claim requires ISSUE and the last-seen OID" % command)
        ttl = _ttl(args[2] if len(args) > 2 else None)
        if command == "renew":
            print(renew(issue, who, ttl, args[1], cwd))
        else:
            heartbeat(issue, who, ttl, args[1], cwd, sys.stdout)
        return 0
    except (ValueError, OSError) as exc:
        detail = _refusal(issue, cwd, str(exc)) if issue else str(exc)
        sys.stderr.write("REFUSED: %s\n" % detail)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
