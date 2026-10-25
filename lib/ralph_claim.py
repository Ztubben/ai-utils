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
import signal
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
# How many times a compare-and-swap on a Claim that lost its race is retried
# before the caller is told. A fenced mutation renews the Claim too (#139), so
# a holder's own lease moves under its heartbeat; each retry re-reads the ref.
CAS_RETRIES = 3
# A mutation refused because the calling Slot no longer holds the issue's
# Claim (#139). Its own code, distinct from every other refusal (2) and from
# the codes the agent launcher and the review stages already use, because the
# Tick must act on it differently: abandon the unit of work, record no Attempt
# and write nothing further. See docs/agents/tick.md.
EXIT_CLAIM_LOST = 21
# The issue this Slot took a Claim on, exported by the Tick and inherited by
# every subcommand and agent it runs. It is what tells the fence that an
# *absent* Claim is a lost one: a ref that is gone reads the same whether
# nobody ever claimed the issue or a replacement holder took it over and then
# released it at the end of its own unit, and only the caller knows which (#139).
CLAIMED_ENV = "RALPH_CLAIM_ISSUE"


class ClaimLost(ValueError):
    """This Slot does not hold the Claim on the issue it is trying to change."""


@dataclass(frozen=True)
class Lease:
    holder: str
    expiry: float


@dataclass
class Plan:
    ok: bool = True
    errors: list = field(default_factory=list)
    commands: list = field(default_factory=list)


@dataclass(frozen=True)
class Fenced:
    """What a mutation fenced on the caller's Claim may do (#139)."""
    ok: bool
    renew: bool = False
    error: str = None


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


def fence_decision(issue, who, lease, claimed=None):
    """Whether `who` may still change `issue` (ADR-0008, #139).

    A Slot that has lost its Claim can no longer change the issue, so every
    mutating subcommand asks this first. Three answers:

    * no Claim at all -- pass, unfenced, *unless* `claimed` says this caller
      took one on this very issue. The fence refuses a *lost* Claim, not a
      mutation nobody claimed: the completion, arbitration and unpark passes
      hold no Claim of their own, and refusing them would stop the Loop. But a
      Slot that did hold one and finds the ref gone has lost it -- the Slot
      that took the issue over has since released it at the end of its own
      unit -- and must write nothing more.
    * another holder -- refuse, naming it, so the Tick can log who took over.
    * this holder -- pass, *by renewing*. The compare-and-swap is the check
      (#137): a takeover inside this window makes the renewal fail, and the
      mutation is refused then. A second read could not be atomic with it.
    """
    if lease is None:
        if claimed is not None and str(claimed) == str(issue):
            return Fenced(False, error="claim #%s is no longer held by %s (it "
                                       "was taken over and released)"
                                       % (issue, who))
        return Fenced(True)
    if lease.holder != who:
        return Fenced(False, error="claim #%s held by %s" % (issue, lease.holder))
    return Fenced(True, renew=True)


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


def _moved_by_a_fence(issue, who, last_seen, cwd):
    """The object id to swap from when a compare-and-swap found something other
    than the value this caller last wrote, or None when it must not retry.

    A fenced mutation renews the Claim too (#139), so a holder's own lease
    moves under its heartbeat and under the release that ends its unit of
    work. Only this holder's *live* lease may be swapped from again: a Claim
    another Slot has taken over is never overwritten, and one that has lapsed
    is never resurrected (#137). An id equal to the one that just failed means
    the ref did not move at all, so the failure was not a lost race.
    """
    try:
        oid, lease = observe(issue, cwd)
    except (ValueError, OSError):
        return None
    if not oid or oid == last_seen or lease.holder != who:
        return None
    return None if expired(lease, time.time()) else oid


def _swap(plan, issue, who, last_seen, cwd):
    """Run one compare-and-swap on a Claim, re-reading the ref when it loses.

    A holder's own lease legitimately moves under it: every fenced mutation
    renews the Claim (#139), so the value the heartbeat last wrote -- and the
    one the release at the end of the unit of work deletes against -- can be
    stale through no fault of its own. Each retry swaps from what the ref
    actually holds, and only while that is still this holder's live lease, so
    another Slot's Claim is never overwritten and a lapsed one is never
    resurrected (#137). Bounded, because losing every race in a row means
    something other than a fence is writing the ref.
    """
    for attempt in range(CAS_RETRIES + 1):
        try:
            return run_plan(plan(last_seen), cwd)
        except ValueError:
            moved = (None if attempt == CAS_RETRIES
                     else _moved_by_a_fence(issue, who, last_seen, cwd))
            if not moved:
                raise
            last_seen = moved


def renew(issue, who, ttl, last_seen, cwd):
    """One heartbeat: a fresh expiry swapped in for the value last written."""
    oid = write_lease(who, ttl, cwd)
    _swap(lambda last: renew_plan(issue, oid, last), issue, who, last_seen, cwd)
    return oid


def fence(issue, who, ttl, cwd, claimed=None):
    """Refuse a mutation of `issue` by a Slot that has lost its Claim (#139).

    Fails closed: a Claim this Slot cannot verify is not a Claim it holds, and
    a Tick that cannot reach origin could do nothing useful with the issue
    anyway -- so a read or push failure refuses as well, naming its reason and
    whatever holder can still be read.
    """
    try:
        observed_oid, lease = observe(issue, cwd)
        decision = fence_decision(issue, who, lease, claimed)
        if not decision.ok:
            raise ClaimLost(decision.error)
        if decision.renew:
            renew(issue, who, ttl, observed_oid, cwd)
    except ClaimLost:
        raise
    except (ValueError, OSError) as exc:
        raise ClaimLost(_refusal(issue, cwd, str(exc))) from exc


def fence_mutation(issue, cwd, ttl=None, env=None):
    """Ask the fence again, from inside a subcommand that is already running.

    The dispatcher fences a subcommand once, before it starts (`bin/ralph`).
    That is enough for the short ones, and not enough for the review path
    (#139, F-2 of PR #160 round 1): `--await-review` sleeps between polls and
    waits out whole agent invocations -- a suspended machine being the case
    this fence exists for -- and both the review round and the response
    publish only once the agent has returned. Each of those writes asks here
    first, so a Story taken over mid-window is not published to, answered on
    or completed by its former holder. Raises `ClaimLost`; unfenced on exactly
    the dispatcher's terms, so without `RALPH_SLOT` it does nothing at all.
    """
    env = os.environ if env is None else env
    if not env.get(ralph_slot.SLOT_ENV):
        return None
    fence(issue, holder(env), _fence_ttl(None) if ttl is None else ttl, cwd,
          env.get(CLAIMED_ENV))
    return None


def _alive(pid):
    """Whether a process we do not own as a child is still around."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tick_alive(interval, parent, tick, stopping):
    """Sleep out one interval, unless the unit of work has ended: the Tick was
    asked to stop us, the process that started us went away, or the Tick
    itself did."""
    deadline = time.time() + interval
    while True:
        if stopping or os.getppid() != parent or not _alive(tick):
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(POLL, remaining))


def heartbeat(issue, who, ttl, oid, cwd, out, tick=None):
    """Keep a Claim alive for as long as its unit of work runs, and no longer.
    A Tick killed outright runs no trap, and an orphan still renewing would
    lock its issue away from every other Slot for as long as the machine stays
    up, so the heartbeat watches both the process that started it and the
    *Tick* it belongs to -- which is not the same process, because the Tick
    starts it inside the `work_backlog` subshell and that subshell outlives a
    kill of the Tick. Each oid swapped to is reported, because releasing the
    Claim must name the last one; a TERM is therefore a request to stop at the
    next safe point, never mid-renewal -- origin may already have accepted a
    renewal whose oid nobody has been told about yet."""
    interval, parent = heartbeat_interval(ttl), os.getppid()
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    while _tick_alive(interval, parent, tick or parent, stopping):
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


COMMANDS = ("claim", "renew", "heartbeat", "release", "verify", "list",
            "fence", "holder")
USAGE = ("usage: ralph_claim.py {claim ISSUE [CONFIG]|renew ISSUE OID [CONFIG]"
         "|heartbeat ISSUE OID [CONFIG]|release ISSUE OID|verify ISSUE|list"
         "|fence ISSUE [CONFIG]|holder ISSUE}\n")


def _tick_pid(env):
    """The Tick a heartbeat belongs to. Unset (a hand-run heartbeat), its own
    parent is the best it knows."""
    value = env.get("RALPH_TICK_PID")
    if value is None:
        return None
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError("RALPH_TICK_PID: expected a process id")
    return int(value)


def _ttl(path):
    config = ralph_config.load_and_validate(path or ".ralph.yml")
    if not config.ok:
        raise ValueError("; ".join(config.errors))
    return config.config["concurrency"]["claim_ttl"]


def _fence_ttl(path):
    """The lease a fenced renewal writes. The fence is a pre-step of whatever
    subcommand it guards, and those carry their config argument in different
    positions, so it reads the target repository's `.ralph.yml` when it can
    and falls back to the lease the schema ships -- never a hard-coded one."""
    try:
        return _ttl(path)
    except (ValueError, OSError):
        return ralph_config.defaults()["concurrency"]["claim_ttl"]


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
            # A fenced mutation of this Slot's own may have moved the ref
            # since the Claim was taken or last renewed (#139).
            _swap(lambda last: release_plan(issue, last), issue, who, args[1], cwd)
            return 0
        if command == "fence":
            if len(args) > 2:
                raise ValueError("fence requires ISSUE [CONFIG]")
            fence(issue, who, _fence_ttl(args[1] if len(args) > 1 else None), cwd,
                  os.environ.get(CLAIMED_ENV))
            return 0
        if command == "holder":
            # Read-only, for the Tick's "Claim lost" log line: an unclaimed
            # issue prints nothing and is still a success.
            if len(args) != 1:
                raise ValueError("claim-holder requires ISSUE")
            _, lease = observe(issue, cwd)
            if lease:
                print(lease.holder)
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
            heartbeat(issue, who, ttl, args[1], cwd, sys.stdout,
                      _tick_pid(os.environ))
        return 0
    except ClaimLost as exc:
        # Already names the holder and the reason; its own exit code, so the
        # Tick can tell "another Slot owns this now" from any other refusal.
        sys.stderr.write("REFUSED: %s\n" % exc)
        return EXIT_CLAIM_LOST
    except (ValueError, OSError) as exc:
        detail = _refusal(issue, cwd, str(exc)) if issue else str(exc)
        sys.stderr.write("REFUSED: %s\n" % detail)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
