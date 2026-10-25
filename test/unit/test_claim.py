"""Story Claims (#136, #137): pure plans and real git against a bare origin."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import ralph_claim
from test_frozen_tooling import (FULL_CONFIG, GIT_ENV, SelfHostedWorld,
                                 _write_exec, ready_story)

RALPH = os.path.join(ROOT, "bin", "ralph")
A = "a" * 40
B = "b" * 40
NOW = 1700000000.0


HOST = socket.gethostname()


def alive(pid):
    """A reparented process that has exited may linger as a zombie."""
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(") ", 1)[1].split()[0] not in ("Z", "X")
    except FileNotFoundError:
        return False


def parent_of(pid):
    with open("/proc/%d/stat" % pid) as fh:
        return int(fh.read().rsplit(") ", 1)[1].split()[1])


class ClaimPlans(unittest.TestCase):
    def test_lease_round_trip(self):
        lease = ralph_claim.Lease("machine:2", 1700000600)
        self.assertEqual(ralph_claim.decode(ralph_claim.encode(lease)), lease)

    def test_malformed_leases_are_refused(self):
        for value in ("{}", "not json", '{"holder":"","expiry":10}',
                      '{"holder":"machine:1","expiry":"tomorrow"}'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ralph_claim.decode(value)

    def test_claim_creates_only_if_absent_outside_branches(self):
        plan = ralph_claim.claim_plan(136, A)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [["git", "push", "--porcelain",
                         "--force-with-lease=refs/ralph/claims/136:", "origin",
                         A + ":refs/ralph/claims/136"]])
        self.assertNotIn("refs/heads/", repr(plan.commands))

    def test_a_claim_still_held_is_refused_and_names_its_holder(self):
        lease = ralph_claim.Lease("another:1", NOW)
        plan = ralph_claim.claim_plan(136, B, lease, A, NOW - 1)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertIn("another:1", " ".join(plan.errors))

    def test_release_deletes_only_the_last_seen_value(self):
        plan = ralph_claim.release_plan(136, A)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [["git", "push", "--porcelain",
                         "--force-with-lease=refs/ralph/claims/136:" + A,
                         "origin", ":refs/ralph/claims/136"]])

    def test_verify_plans_a_remote_read(self):
        self.assertEqual(ralph_claim.verify_plan(136).commands,
                         [["git", "ls-remote", "origin", "refs/ralph/claims/136"]])

    def test_invalid_issue_or_release_value_cannot_build_a_command(self):
        for issue in ("../heads/main", "--all", 0):
            self.assertFalse(ralph_claim.claim_plan(issue, A).ok)
        self.assertFalse(ralph_claim.release_plan(136, "").ok)

    def test_the_heartbeat_interval_derives_from_the_lease(self):
        # ADR-0008: a ten-minute lease is renewed about every two minutes.
        self.assertEqual(ralph_claim.heartbeat_interval(600), 120)
        self.assertEqual(ralph_claim.heartbeat_interval(10), 2)
        self.assertLess(ralph_claim.heartbeat_interval(1), 1)
        for ttl in (0, -600, "soon", None, True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                ralph_claim.heartbeat_interval(ttl)

    def test_a_lease_expires_at_its_expiry(self):
        lease = ralph_claim.Lease("machine:1", NOW)
        self.assertFalse(ralph_claim.expired(lease, NOW - 1))
        self.assertTrue(ralph_claim.expired(lease, NOW))
        self.assertTrue(ralph_claim.expired(lease, NOW + 1))

    def test_an_expired_claim_is_taken_over_from_the_value_observed(self):
        lease = ralph_claim.Lease("another:1", NOW)
        plan = ralph_claim.claim_plan(136, B, lease, A, NOW + 1)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [["git", "push", "--porcelain",
                         "--force-with-lease=refs/ralph/claims/136:" + A,
                         "origin", B + ":refs/ralph/claims/136"]])

    def test_a_takeover_without_the_observed_value_cannot_build_a_command(self):
        lease = ralph_claim.Lease("another:1", NOW)
        self.assertFalse(ralph_claim.claim_plan(136, B, lease, None, NOW + 1).ok)

    def test_renewal_swaps_from_the_value_last_written(self):
        plan = ralph_claim.renew_plan(136, B, A)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [["git", "push", "--porcelain",
                         "--force-with-lease=refs/ralph/claims/136:" + A,
                         "origin", B + ":refs/ralph/claims/136"]])
        # A renewal is a compare-and-swap, never a create.
        self.assertFalse(ralph_claim.renew_plan(136, B, "").ok)
        self.assertFalse(ralph_claim.renew_plan(136, "", A).ok)


class ClaimsOnGit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = SelfHostedWorld(self.tmp.name)

    def cli(self, *args, slot="1", cwd=None):
        return subprocess.run([RALPH] + list(args), cwd=cwd or self.world.checkout,
                              env=dict(self.world.env(), RALPH_SLOT=slot), text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    def test_simultaneous_claims_have_one_winner_and_report_the_holder(self):
        # Both callers start from independent clones and race on the same ref.
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.cli, "--claim", "136", slot=slot, cwd=cwd)
                     for slot, cwd in (("1", self.world.checkout),
                                       ("2", self.world.upstream))]
            results = [call.result() for call in calls]
        self.assertEqual(sorted(p.returncode for p in results), [0, 2],
                         [(p.stdout, p.stderr) for p in results])
        winner = next(i for i, p in enumerate(results) if p.returncode == 0)
        holder = socket.gethostname() + ":" + str(winner + 1)
        loser = results[1 - winner]
        self.assertIn(holder, loser.stderr)
        oid = self.world.git("ls-remote", "origin", "refs/ralph/claims/136").split()[0]
        self.assertEqual(results[winner].stdout.strip(), oid)
        raw = self.world.git("show", "-s", "--format=%B", oid,
                             cwd=(self.world.checkout, self.world.upstream)[winner])
        self.assertEqual(json.loads(raw)["holder"], holder)
        branches = self.world.git("ls-remote", "--heads", "origin")
        self.assertNotIn("claims", branches)

    def test_verify_release_and_a_stale_delete(self):
        got = self.cli("--claim", "136")
        self.assertEqual(got.returncode, 0, got.stderr)
        oid = got.stdout.strip()
        verified = self.cli("--verify-claim", "136")
        self.assertEqual(verified.returncode, 0, verified.stderr)
        wrong_holder = self.cli("--verify-claim", "136", slot="2")
        self.assertEqual(wrong_holder.returncode, 2)
        self.assertIn(socket.gethostname() + ":1", wrong_holder.stderr)
        self.assertEqual(self.cli("--claimed-issues", slot="2").stdout.strip(), "136")
        self.assertEqual(self.cli("--claimed-issues").stdout.strip(), "")
        self.assertEqual(self.cli("--release-claim", "136", oid).returncode, 0)
        newer = self.cli("--claim", "136", slot="2")
        self.assertEqual(newer.returncode, 0, newer.stderr)
        stale = self.cli("--release-claim", "136", oid)
        self.assertEqual(stale.returncode, 2)
        self.assertIn(socket.gethostname() + ":2", stale.stderr)
        self.assertIn(newer.stdout.strip(), self.world.git("ls-remote", "origin",
                                                         "refs/ralph/claims/136"))

    def test_tick_holds_claim_during_iteration_and_detaches_on_release(self):
        self.world.set_backlog([ready_story()])
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
ralph --verify-claim "$RALPH_ITERATION_ISSUE" || exit 64
echo "claim-held branch=$(git branch --show-current)" >> "$RALPH_LOG"
""")
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("claim-held branch=ralph/7-s7", self.world.log_lines(), proc.stdout)
        self.assertEqual(self.world.git("ls-remote", "origin", "refs/ralph/claims/*"), "")
        self.assertEqual(self.world.git("branch", "--show-current",
                                       cwd=self.world.checkout + ".ralph-slot-1"), "")

    def test_tick_skips_another_holders_story_and_works_the_next(self):
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        self.world.set_backlog([ready_story(7), ready_story(8)])
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
echo "worked=$RALPH_ITERATION_ISSUE" >> "$RALPH_LOG"
""")
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("worked=8", self.world.log_lines(), proc.stdout)
        self.assertNotIn("worked=7", self.world.log_lines())
        refs = self.world.git("ls-remote", "origin", "refs/ralph/claims/*")
        self.assertIn("refs/ralph/claims/7", refs)
        self.assertNotIn("refs/ralph/claims/8", refs)

    def config(self, ttl):
        """A config whose lease is short enough for a test to outlive it."""
        path = os.path.join(self.tmp.name, "ttl-%d.yml" % ttl)
        with open(FULL_CONFIG) as src, open(path, "w") as fh:
            fh.write(src.read() + "concurrency:\n  claim_ttl: %d\n" % ttl)
        return path

    def heartbeat(self, issue, oid, config, cwd=None, wrap=None, env=None):
        """The heartbeat as the Tick runs it: a child writing the oids it swaps
        to, so whoever releases the Claim can name the lease last written."""
        out = os.path.join(self.tmp.name, "oids-%s" % issue)
        argv = [RALPH, "--heartbeat-claim", str(issue), oid, config]
        with open(out, "w") as sink:
            proc = subprocess.Popen((wrap or []) + argv, cwd=cwd or self.world.checkout,
                                    env=dict(self.world.env(), RALPH_SLOT="1",
                                             **(env or {})),
                                    stdout=sink, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, proc)
        return proc, out

    def stop(self, proc):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stderr.close()

    def until(self, condition, what, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail("timed out waiting until " + what)

    def renewed(self, path):
        """The object ids a heartbeat has reported swapping to, oldest first."""
        with open(path) as fh:
            return fh.read().split()

    def leases(self, path, cwd=None):
        return [json.loads(self.world.git("show", "-s", "--format=%B", oid, cwd=cwd))
                for oid in self.renewed(path)]

    def test_a_heartbeat_keeps_a_claim_past_its_lease(self):
        config = self.config(1)
        got = self.cli("--claim", "136", config)
        self.assertEqual(got.returncode, 0, got.stderr)
        oid = got.stdout.strip()
        taken = json.loads(self.world.git("show", "-s", "--format=%B", oid))
        proc, out = self.heartbeat("136", oid, config)
        time.sleep(1.4)   # past the lease the Claim was taken with
        self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() else "")
        renewals = self.leases(out)
        self.assertGreater(len(renewals), 1, renewals)
        self.assertEqual([r["holder"] for r in renewals], [taken["holder"]] * len(renewals))
        # Each heartbeat advances the expiry, and the live Claim stays this
        # Slot's: another Slot asking for it is still refused, long after the
        # lease it was first taken with ran out.
        self.assertEqual(sorted(r["expiry"] for r in renewals),
                         [r["expiry"] for r in renewals])
        self.assertGreater(renewals[-1]["expiry"], taken["expiry"])
        remote = self.world.git("ls-remote", "origin", "refs/ralph/claims/136").split()[0]
        self.assertEqual(remote, self.renewed(out)[-1])
        refused = self.cli("--claim", "136", config, slot="2", cwd=self.world.upstream)
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn(HOST + ":1", refused.stderr)

    def test_an_expired_claim_is_taken_over_and_the_old_holder_cannot_renew(self):
        # Long enough that the reads below cannot themselves outlive the lease.
        config = self.config(2)
        got = self.cli("--claim", "136", config)
        self.assertEqual(got.returncode, 0, got.stderr)
        oid = got.stdout.strip()
        early = self.cli("--claim", "136", config, slot="2", cwd=self.world.upstream)
        self.assertEqual(early.returncode, 2, early.stdout)
        self.assertIn(HOST + ":1", early.stderr)
        self.assertEqual(self.cli("--claimed-issues", slot="2").stdout.strip(), "136")
        time.sleep(2.2)
        # A crashed or suspended holder releases its issue within one lease: the
        # Claim no longer counts as held, and another Slot takes it over.
        self.assertEqual(self.cli("--claimed-issues", slot="2").stdout.strip(), "")
        taken = self.cli("--claim", "136", config, slot="2", cwd=self.world.upstream)
        self.assertEqual(taken.returncode, 0, taken.stderr)
        self.assertEqual(json.loads(self.world.git("show", "-s", "--format=%B",
                                                   taken.stdout.strip(),
                                                   cwd=self.world.upstream))["holder"],
                         HOST + ":2")
        # The old holder's heartbeat must not resurrect its lease.
        stale = self.cli("--renew-claim", "136", oid, config)
        self.assertEqual(stale.returncode, 2, stale.stdout)
        self.assertIn(HOST + ":2", stale.stderr)
        self.assertIn(taken.stdout.strip(),
                      self.world.git("ls-remote", "origin", "refs/ralph/claims/136"))

    def test_simultaneous_takeovers_of_an_expired_claim_have_one_winner(self):
        config = self.config(1)
        self.assertEqual(self.cli("--claim", "136", config, slot="3").returncode, 0)
        time.sleep(1.1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(self.cli, "--claim", "136", config, slot=slot, cwd=cwd)
                     for slot, cwd in (("1", self.world.checkout),
                                       ("2", self.world.upstream))]
            results = [call.result() for call in calls]
        self.assertEqual(sorted(p.returncode for p in results), [0, 2],
                         [(p.stdout, p.stderr) for p in results])
        winner = next(i for i, p in enumerate(results) if p.returncode == 0)
        self.assertIn(HOST + ":" + str(winner + 1), results[1 - winner].stderr)
        self.assertIn(results[winner].stdout.strip(),
                      self.world.git("ls-remote", "origin", "refs/ralph/claims/136"))

    def test_a_heartbeat_never_outlives_the_tick_that_started_it(self):
        config = self.config(1)
        oid = self.cli("--claim", "136", config).stdout.strip()
        # A Tick killed outright runs no trap: the heartbeat must stop itself.
        pidfile = os.path.join(self.tmp.name, "heartbeat.pid")
        proc, out = self.heartbeat(
            "136", oid, config,
            wrap=["bash", "-c", '"$@" & echo $! >"$0"; wait', pidfile])
        self.until(lambda: os.path.getsize(out), "the heartbeat renewed once")
        with open(pidfile) as fh:
            child = int(fh.read())
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        self.until(lambda: not alive(child), "the heartbeat stopped with its Tick")
        renewals = os.path.getsize(out)
        time.sleep(0.6)   # three renewal intervals of the lease above
        self.assertEqual(os.path.getsize(out), renewals)

    def test_a_heartbeat_never_outlives_the_tick_even_in_a_subshell(self):
        # The Tick starts its heartbeat inside `work_backlog`, which is a
        # subshell, so the heartbeat's parent is *not* the Tick: a Tick killed
        # outright leaves that subshell behind, and a heartbeat watching only
        # its immediate parent would go on renewing a Claim no other Slot
        # could ever take over. It watches the Tick it was told about (#137).
        config = self.config(1)
        oid = self.cli("--claim", "136", config).stdout.strip()
        pidfile = os.path.join(self.tmp.name, "subshell-heartbeat.pid")
        proc, out = self.heartbeat(
            "136", oid, config,
            wrap=["bash", "-c",
                  '( export RALPH_TICK_PID=$$; "$@" & echo $! >"$0"; wait )',
                  pidfile])
        self.until(lambda: os.path.getsize(out), "the heartbeat renewed once")
        with open(pidfile) as fh:
            child = int(fh.read())
        subshell = parent_of(child)
        self.assertNotEqual(subshell, proc.pid, "the Tick is not the parent")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
        self.until(lambda: not alive(child), "the heartbeat stopped with its Tick")

    def slow_push(self, seconds="2"):
        """A `git` whose push returns only once the remote has long accepted
        it: the window in which the Claim has already moved to an object id
        the heartbeat has not reported yet."""
        shim = os.path.join(self.tmp.name, "slow-git")
        os.makedirs(shim, exist_ok=True)
        mark = os.path.join(self.tmp.name, "pushed")
        _write_exec(os.path.join(shim, "git"), """#!/usr/bin/env bash
if [[ "$1" == push ]]; then
  "$SLOW_REAL_GIT" "$@"; rc=$?
  echo accepted >> "$SLOW_PUSH_MARK"
  sleep %s
  exit "$rc"
fi
exec "$SLOW_REAL_GIT" "$@"
""" % seconds)
        return {"PATH": shim + os.pathsep + self.world.env()["PATH"],
                "SLOW_REAL_GIT": shutil.which("git"),
                "SLOW_PUSH_MARK": mark}, mark

    def test_a_renewal_in_flight_is_reported_before_the_unit_of_work_ends(self):
        # Stopping the heartbeat must not lose a renewal origin has already
        # accepted: that object id *is* the Claim, and a release naming the
        # previous one is refused, stranding the ref until it expires (#137).
        config = self.config(1)
        oid = self.cli("--claim", "136", config).stdout.strip()
        env, mark = self.slow_push()
        proc, out = self.heartbeat("136", oid, config, env=env)
        self.until(lambda: os.path.exists(mark), "origin accepted a renewal")
        proc.terminate()   # the unit of work ends with that renewal in flight
        proc.wait(timeout=30)
        swapped = self.renewed(out)
        self.assertTrue(swapped, proc.stderr.read())
        remote = self.world.git("ls-remote", "origin", "refs/ralph/claims/136").split()[0]
        self.assertNotEqual(remote, oid, "the renewal did reach origin")
        self.assertEqual(swapped[-1], remote)
        # ...so the release the Tick makes next names the live lease.
        released = self.cli("--release-claim", "136", swapped[-1])
        self.assertEqual(released.returncode, 0, released.stderr)

    def test_a_long_iteration_renews_its_claim_and_still_releases_it(self):
        # The lease the Slot reads is origin/main's, so the short one lands there.
        self.world.merge_to_main(".ralph.yml", "concurrency:\n  claim_ttl: 1\n",
                                 "a lease an iteration outlives")
        self.world.set_backlog([ready_story()])
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
lease() { git ls-remote origin "refs/ralph/claims/$RALPH_ITERATION_ISSUE" | cut -f1; }
echo "lease-before=$(lease)" >> "$RALPH_LOG"
sleep 1.4
echo "lease-after=$(lease)" >> "$RALPH_LOG"
""")
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = self.world.log_lines()
        before = next(ln for ln in log if ln.startswith("lease-before="))
        after = next(ln for ln in log if ln.startswith("lease-after="))
        self.assertTrue(after.split("=", 1)[1], log)
        # The expiry advanced under the agent, so the Claim never lapsed...
        self.assertNotEqual(before.split("=", 1)[1], after.split("=", 1)[1], log)
        # ...and the release named the lease the heartbeat last wrote, not the
        # stale one the Claim was taken with.
        self.assertEqual(self.world.git("ls-remote", "origin", "refs/ralph/claims/*"), "")
        self.assertNotIn("could not release", proc.stdout)
