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

    def test_a_mutation_by_the_holder_is_fenced_by_a_renewal(self):
        # The renewal *is* the check (#137): a takeover inside the window makes
        # the compare-and-swap fail, so the fence needs no second read.
        decision = ralph_claim.fence_decision(136, "machine:1",
                                              ralph_claim.Lease("machine:1", NOW))
        self.assertTrue(decision.ok, decision.error)
        self.assertTrue(decision.renew)

    def test_a_mutation_by_another_slot_is_refused_naming_the_holder(self):
        decision = ralph_claim.fence_decision(136, "machine:1",
                                              ralph_claim.Lease("machine:2", NOW))
        self.assertFalse(decision.ok)
        self.assertFalse(decision.renew)
        self.assertIn("machine:2", decision.error)
        self.assertIn("136", decision.error)

    def test_a_mutation_on_an_unclaimed_issue_is_unfenced(self):
        # The fence refuses a *lost* Claim, not a mutation nobody claimed: the
        # completion, arbitration and unpark passes hold no Claim of their own.
        decision = ralph_claim.fence_decision(136, "machine:1", None)
        self.assertTrue(decision.ok, decision.error)
        self.assertFalse(decision.renew, "nothing to renew")

    def test_a_claim_this_slot_was_holding_and_that_is_gone_is_lost(self):
        # F-1 of round 1: a replacement holder releases at the end of its unit,
        # so the ref is absent again. Absent means "unclaimed" to a pass that
        # never held a Claim and "lost" to the Slot that did -- and only the
        # caller knows which it is, which is what `claimed` tells the fence.
        decision = ralph_claim.fence_decision(136, "machine:1", None, claimed="136")
        self.assertFalse(decision.ok)
        self.assertFalse(decision.renew)
        self.assertIn("136", decision.error)
        self.assertIn("machine:1", decision.error)
        # Another issue's Claim says nothing about this one.
        self.assertTrue(ralph_claim.fence_decision(136, "machine:1", None,
                                                   claimed="137").ok)

    def test_a_mutation_is_unfenced_inside_a_process_that_holds_no_slot(self):
        # The in-process re-fence (#139, F-2) is the dispatcher's fence asked
        # again from inside a long-running subcommand, so it is unfenced on
        # exactly the same terms: without RALPH_SLOT, nothing is fenced.
        self.assertIsNone(ralph_claim.fence_mutation(136, ".", env={}))

    def test_the_lost_claim_exit_code_is_its_own(self):
        # The Tick tells a lost Claim from every other refusal by this code.
        self.assertEqual(ralph_claim.EXIT_CLAIM_LOST, 21)
        self.assertNotIn(ralph_claim.EXIT_CLAIM_LOST, (0, 1, 2))

    def test_renewal_swaps_from_the_value_last_written(self):
        plan = ralph_claim.renew_plan(136, B, A)
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [["git", "push", "--porcelain",
                         "--force-with-lease=refs/ralph/claims/136:" + A,
                         "origin", B + ":refs/ralph/claims/136"]])
        # A renewal is a compare-and-swap, never a create.
        self.assertFalse(ralph_claim.renew_plan(136, B, "").ok)
        self.assertFalse(ralph_claim.renew_plan(136, "", A).ok)


def feature_story(number, parent):
    story = ready_story(number)
    story["body"] = story["body"].replace("Parent: None", "Parent: #%d" % parent)
    return story


def prd(number):
    return {"number": number, "title": "P%d" % number, "state": "OPEN",
            "labels": [{"name": "prd"}], "body": "A Feature.\n"}


class FeatureClaims(unittest.TestCase):
    """One Slot per Feature (#141): which PRD a unit of work also claims."""

    def test_a_feature_story_also_claims_its_prd_by_default(self):
        self.assertEqual(ralph_claim.feature_claim(feature_story(11, 10), True), 10)

    def test_an_orphan_story_holds_only_its_own_claim(self):
        self.assertIsNone(ralph_claim.feature_claim(ready_story(7), True))

    def test_a_prd_claims_nothing_more(self):
        self.assertIsNone(ralph_claim.feature_claim(prd(10), True))

    def test_lifting_exclusivity_takes_only_the_storys_own_claim(self):
        self.assertIsNone(ralph_claim.feature_claim(feature_story(11, 10), False))

    def test_the_cli_reads_the_story_and_the_configs_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            story = os.path.join(tmp, "story.json")
            with open(story, "w") as fh:
                json.dump(feature_story(11, 10), fh)
            lifted = os.path.join(tmp, "lifted.yml")
            with open(FULL_CONFIG) as src, open(lifted, "w") as fh:
                fh.write(src.read() + "concurrency:\n  feature_exclusive: false\n")
            for config, expected in ((FULL_CONFIG, "10"), (lifted, "")):
                with self.subTest(config=config):
                    proc = subprocess.run([RALPH, "--feature-claim", story, config],
                                          text=True, capture_output=True)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(proc.stdout.strip(), expected)


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

    def verifying_agents(self):
        """Agents that log which Claims their Slot holds while they work."""
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
for issue in "$RALPH_ITERATION_ISSUE" 10; do
  if ralph --verify-claim "$issue" >/dev/null 2>&1; then
    echo "holds=$issue" >> "$RALPH_LOG"
  fi
done
echo "worked=$RALPH_ITERATION_ISSUE" >> "$RALPH_LOG"
""")

    def test_a_feature_storys_unit_holds_its_prds_claim_and_releases_both(self):
        self.world.set_backlog([prd(10), feature_story(11, 10)])
        self.verifying_agents()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        lines = self.world.log_lines()
        self.assertIn("holds=11", lines, proc.stdout)
        self.assertIn("holds=10", lines, proc.stdout)
        self.assertEqual(self.world.git("ls-remote", "origin", "refs/ralph/claims/*"), "")

    def test_an_orphan_storys_unit_holds_only_its_own_claim(self):
        self.world.set_backlog([prd(10), ready_story(7)])
        self.verifying_agents()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("holds=7", self.world.log_lines(), proc.stdout)
        self.assertNotIn("holds=10", self.world.log_lines())

    def test_lifted_exclusivity_takes_only_the_storys_own_claim(self):
        self.world.merge_to_main(".ralph.yml", "concurrency:\n  feature_exclusive: false\n")
        self.world.set_backlog([prd(10), feature_story(11, 10)])
        self.verifying_agents()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("holds=11", self.world.log_lines(), proc.stdout)
        self.assertNotIn("holds=10", self.world.log_lines())

    def test_a_tick_skips_a_feature_whose_prd_another_holder_claims(self):
        self.assertEqual(self.cli("--claim", "10", slot="2").returncode, 0)
        self.world.set_backlog([prd(10), feature_story(11, 10), ready_story(12)])
        self.verifying_agents()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("worked=12", self.world.log_lines(), proc.stdout)
        self.assertNotIn("worked=11", self.world.log_lines())
        refs = self.world.git("ls-remote", "origin", "refs/ralph/claims/*")
        self.assertEqual(refs.split()[1:], ["refs/ralph/claims/10"])

    def test_losing_the_prd_race_releases_the_storys_claim_and_moves_on(self):
        # Selection saw #10 free, but another Slot claims it while this Tick
        # reads #11 -- after selection, before its own PRD Claim.
        self.world.set_backlog([prd(10), feature_story(11, 10), ready_story(12)])
        self.verifying_agents()
        gh = os.path.join(self.world.mockbin, "gh")
        os.rename(gh, gh + ".real")
        _write_exec(gh, """#!/usr/bin/env bash
if [[ "$1 $2 $3" == "issue view 11" && ! -e "$RALPH_GH_DIR/raced" ]]; then
  touch "$RALPH_GH_DIR/raced"
  RALPH_SLOT=2 %s --claim 10 >/dev/null
fi
exec "%s.real" "$@"
""" % (RALPH, gh))
        # The lost race spends one pass of the Tick; the next works #12.
        proc = self.world.tick(RALPH_MAX_ITERATIONS="2")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("could not claim Feature #10", proc.stdout)
        self.assertIn("worked=12", self.world.log_lines(), proc.stdout)
        self.assertNotIn("worked=11", self.world.log_lines())
        refs = self.world.git("ls-remote", "origin", "refs/ralph/claims/*")
        self.assertEqual(refs.split()[1:], ["refs/ralph/claims/10"])

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

    # --- mutation fencing (#139) ---------------------------------------
    # A Slot that has lost its Claim can no longer change the issue: every
    # mutating subcommand verifies the Claim by compare-and-swap renewal
    # first, and refuses with EXIT_CLAIM_LOST naming the holder.

    def story_file(self, number=7):
        path = os.path.join(self.tmp.name, "story-%d.json" % number)
        with open(path, "w") as fh:
            json.dump(ready_story(number), fh)
        return path

    def park(self, *args, slot="1", stdin=None, env=None):
        """`--park-story`, standing for every mutating subcommand: it writes to
        GitHub alone, so the mock gh's log says exactly whether it ran."""
        story = self.story_file()
        argv = [RALPH, "--park-story", "-" if stdin else story,
                "do the thing", os.path.join(self.world.checkout, ".ralph.yml")]
        full = dict(self.world.env(), **(env or {}))
        if slot is None:
            full.pop("RALPH_SLOT", None)
        else:
            full["RALPH_SLOT"] = slot
        with open(story) as fh:
            piped = fh.read() if stdin else None
        return subprocess.run(argv, cwd=self.world.checkout, env=full, text=True,
                              input=piped, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=30)

    def mutated(self):
        return [ln for ln in self.world.log_lines()
                if ln.startswith("gh issue edit") or ln.startswith("gh issue comment")]

    def claim_oid(self):
        listing = self.world.git("ls-remote", "origin", "refs/ralph/claims/7")
        return listing.split()[0] if listing else ""

    def test_a_mutation_by_the_claim_holder_passes_and_advances_the_lease(self):
        self.assertEqual(self.cli("--claim", "7").returncode, 0)
        taken = self.claim_oid()
        got = self.park()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.mutated(), "the subcommand ran")
        # The fence verified the Claim *by renewing* it, so the ref moved on.
        self.assertNotEqual(self.claim_oid(), taken)
        self.assertEqual(json.loads(self.world.git(
            "show", "-s", "--format=%B", self.claim_oid()))["holder"], HOST + ":1")

    def test_a_mutation_after_another_slot_took_the_claim_is_refused(self):
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        held = self.claim_oid()
        got = self.park()
        self.assertEqual(got.returncode, ralph_claim.EXIT_CLAIM_LOST, got.stdout)
        self.assertIn(HOST + ":2", got.stderr)
        self.assertIn("7", got.stderr)
        # Nothing was written, and the holder's lease is untouched.
        self.assertEqual(self.mutated(), [])
        self.assertEqual(self.claim_oid(), held)

    def test_a_mutation_on_an_issue_nobody_claims_is_unfenced(self):
        # The fence refuses a *lost* Claim, not a mutation nobody claimed: the
        # completion, arbitration and unpark passes hold no Claim of their own.
        got = self.park()
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.mutated())
        self.assertEqual(self.claim_oid(), "")

    def test_without_a_slot_a_mutating_subcommand_behaves_as_before(self):
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        got = self.park(slot=None)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(self.mutated(), "a hand-run subcommand is unfenced")

    def test_a_fenced_subcommand_reading_its_story_from_stdin(self):
        # The Tick pipes `gh issue view` into these: the fence must resolve the
        # issue from that stream and still hand the whole document on.
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        refused = self.park(stdin=True)
        self.assertEqual(refused.returncode, ralph_claim.EXIT_CLAIM_LOST, refused.stdout)
        self.assertIn(HOST + ":2", refused.stderr)
        self.assertEqual(self.mutated(), [])
        self.assertEqual(self.cli("--release-claim", "7", self.claim_oid(),
                                  slot="2").returncode, 0)
        passed = self.park(stdin=True)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(self.mutated())

    def test_read_only_subcommands_are_never_fenced(self):
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        story = self.story_file()
        config = os.path.join(self.world.checkout, ".ralph.yml")
        for args in (["--lint-story", story], ["--branch-name", story, config],
                     ["--blocked-stories"], ["--claimed-issues"],
                     ["--claim-holder", "7"]):
            with self.subTest(args=args):
                got = self.cli(*args)
                self.assertNotEqual(got.returncode, ralph_claim.EXIT_CLAIM_LOST,
                                    got.stderr)

    def test_the_claim_holder_is_readable_for_the_tick_s_log(self):
        self.assertEqual(self.cli("--claim-holder", "7").stdout.strip(), "")
        self.assertEqual(self.cli("--claim", "7", slot="2").returncode, 0)
        got = self.cli("--claim-holder", "7")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), HOST + ":2")

    def test_a_heartbeat_and_a_release_survive_a_fenced_mutation(self):
        # GOTCHA: the fence renews, so it moves the ref out from under the
        # oid the heartbeat swaps from and the one the release deletes
        # against. Both must recover, or every fenced mutation would kill the
        # heartbeat and strand the Claim until it expired.
        config = self.config(2)
        got = self.cli("--claim", "7", config)
        self.assertEqual(got.returncode, 0, got.stderr)
        oid = got.stdout.strip()
        proc, out = self.heartbeat("7", oid, config)
        self.until(lambda: os.path.getsize(out), "the heartbeat renewed once")
        fenced = self.park()
        self.assertEqual(fenced.returncode, 0, fenced.stderr)
        self.assertNotEqual(self.claim_oid(), self.renewed(out)[-1],
                            "the fence moved the ref under the heartbeat")
        time.sleep(0.9)   # more than two renewal intervals of the lease above
        self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() else "")
        self.until(lambda: self.renewed(out)[-1] == self.claim_oid(),
                   "the heartbeat caught up with the fence's renewals")
        proc.terminate()
        proc.wait(timeout=30)
        released = self.cli("--release-claim", "7", self.renewed(out)[-1])
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(self.claim_oid(), "")

    def taken_over_by_slot_two(self, then_released=False):
        """A mock agent under whose feet another Slot takes the Story over.

        It writes the lease Slot 2 would have written when this Slot's own
        lapsed, which is a lid-close without the waiting: from here on every
        mutation this Tick makes is fenced on a Claim it no longer holds."""
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
ref="refs/ralph/claims/$RALPH_ITERATION_ISSUE"
tree="$(git mktree </dev/null)"
lease="$(printf '{"expiry":%s,"holder":"%s:2"}\\n' "$(( $(date +%s) + 600 ))" \\
         "$(hostname)" | git commit-tree "$tree")"
git push -q --force origin "$lease:$ref"
RELEASED && git push -q --force origin ":$ref"
echo "taken-over=$RALPH_ITERATION_ISSUE" >> "$RALPH_LOG"
""".replace("RELEASED", "true" if then_released else "false"))

    def test_a_tick_that_lost_its_claim_ends_the_unit_without_an_attempt(self):
        # The iteration ends with no done-signal and no Handoff, which is
        # normally a failed Attempt -- but the Claim is gone, so the Story is
        # another Slot's and this Tick must charge it nothing and write
        # nothing. Unfenced, this Tick would comment on, label and possibly
        # block a Story the new holder is working.
        self.world.set_backlog([ready_story()])
        self.taken_over_by_slot_two()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("taken-over=7", self.world.log_lines(), proc.stdout)
        self.assertIn("Claim lost for #7 (held by %s:2)" % HOST, proc.stdout)
        self.assertIn("recording no Attempt", proc.stdout)
        # No Attempt recorded, and no state label moved by it. The one comment
        # left is the token ledger of the invocation that really did run:
        # telemetry of this Slot's own spend, which gates nothing and is no
        # part of the Story's state, so the launch stays unfenced.
        comments = [ln for ln in self.world.log_lines()
                    if ln.startswith("gh issue comment")]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("Token ledger", comments[0])
        self.assertNotIn("state:blocked", " ".join(self.world.log_lines()))
        # The new holder's Claim is left exactly as it found it: this Tick
        # neither renewed it nor released it on its way out.
        self.assertEqual(json.loads(self.world.git(
            "show", "-s", "--format=%B", self.claim_oid()))["holder"], HOST + ":2")
        self.assertNotIn("could not release", proc.stdout)
        # ...and the Slot let the Story branch go, so its holder can have it.
        self.assertEqual(self.world.git("branch", "--show-current",
                                        cwd=self.world.checkout + ".ralph-slot-1"), "")

    def test_a_mutation_after_the_takeover_was_released_is_still_refused(self):
        # F-1 of round 1: Slot 2 takes the Claim over and then releases it at
        # the end of its own unit, so the ref is absent again when Slot 1 wakes.
        # Absent is not "free to write": Slot 1's Claim is gone, and the Story
        # is not its business any more.
        held = self.cli("--claim", "7", slot="2")
        self.assertEqual(held.returncode, 0, held.stderr)
        self.assertEqual(self.cli("--release-claim", "7", held.stdout.strip(),
                                  slot="2").returncode, 0)
        self.assertEqual(self.claim_oid(), "")
        got = self.park(env={ralph_claim.CLAIMED_ENV: "7"})
        self.assertEqual(got.returncode, ralph_claim.EXIT_CLAIM_LOST, got.stdout)
        self.assertIn("7", got.stderr)
        self.assertEqual(self.mutated(), [])
        # The fence refuses; it never takes a Claim of its own on the way out.
        self.assertEqual(self.claim_oid(), "")

    def test_a_tick_whose_claim_was_taken_over_and_released_writes_nothing(self):
        # F-1 at Tick level: the Slot that took the Story over finishes its own
        # unit and releases, so this Tick comes back to an *unclaimed* issue it
        # still believes is its own. Unfenced, it would comment on and label a
        # Story another Slot has already worked.
        self.world.set_backlog([ready_story()])
        self.taken_over_by_slot_two(then_released=True)
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("taken-over=7", self.world.log_lines(), proc.stdout)
        self.assertIn("Claim lost for #7", proc.stdout)
        self.assertIn("recording no Attempt", proc.stdout)
        comments = [ln for ln in self.world.log_lines()
                    if ln.startswith("gh issue comment")]
        self.assertEqual(len(comments), 1, comments)   # the token ledger alone
        self.assertIn("Token ledger", comments[0])
        self.assertNotIn("state:blocked", " ".join(self.world.log_lines()))

    def taken_over_the_instant_the_claim_lands(self):
        """A `git` that hands Slot 2 the Claim the moment Slot 1's lands.

        F-3 of round 1: the Tick's own first write -- `begin_story`'s label
        move -- is a direct `gh` call, so no dispatcher fence sees it, and it
        runs before every fenced subcommand of the unit. This wrapper is the
        takeover at exactly that instant: the push that creates the Claim
        succeeds, and the next thing the Tick does is already too late.
        """
        real = shutil.which("git")
        _write_exec(os.path.join(self.world.mockbin, "git"), """#!/usr/bin/env bash
"%s" "$@"; rc=$?
if (( rc == 0 )) && [[ "$*" == *push* && "$*" == *refs/ralph/claims/* \
      && ! -e "%s" ]]; then
  : > "%s"
  tree="$("%s" mktree </dev/null)"
  lease="$(printf '{"expiry":%%s,"holder":"%%s:2"}\\n' "$(( $(date +%%s) + 600 ))" \
           "$(hostname)" | "%s" commit-tree "$tree")"
  "%s" push -q --force origin "$lease:refs/ralph/claims/7"
fi
exit "$rc"
""" % (real, os.path.join(self.tmp.name, "took-over"),
       os.path.join(self.tmp.name, "took-over"), real, real, real))

    def test_the_ticks_first_label_move_is_fenced_like_every_other_write(self):
        # F-3 of round 1: `begin_story` moves state:ready -> state:in-progress
        # with its own `gh`, before any fenced subcommand runs. A Slot whose
        # Claim was taken over between acquiring it and this write must leave
        # the new holder's labels exactly as they are.
        self.world.set_backlog([ready_story()])
        self.taken_over_the_instant_the_claim_lands()
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("Claim lost for #7 (held by %s:2)" % HOST, proc.stdout)
        self.assertEqual(self.mutated(), [], proc.stdout)
        self.assertNotIn("state:in-progress", " ".join(self.world.log_lines()))
        # Not an Attempt, and no agent was launched on another Slot's Story.
        self.assertNotIn("ralph=", " ".join(self.world.log_lines()))

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

    def test_the_agent_s_own_calls_are_fenced_through_its_environment(self):
        # AC: the agent inherits `RALPH_SLOT`, so the mutating subcommands it
        # runs itself -- `--checkpoint`, `--record-attempt` -- are fenced on
        # the same Claim the Tick is fenced on, with nothing said in its
        # prompt. Here another Slot takes the Story over mid-iteration: both
        # of the agent's writes must be refused with EXIT_CLAIM_LOST.
        self.world.set_backlog([ready_story()])
        for provider in ("claude", "codex"):
            _write_exec(os.path.join(self.world.mockbin, provider), """#!/usr/bin/env bash
cat >/dev/null
echo "slot=${RALPH_SLOT:-unset}" >> "$RALPH_LOG"
ref="refs/ralph/claims/$RALPH_ITERATION_ISSUE"
tree="$(git mktree </dev/null)"
lease="$(printf '{"expiry":%s,"holder":"%s:2"}\\n' "$(( $(date +%s) + 600 ))" \\
         "$(hostname)" | git commit-tree "$tree")"
git push -q --force origin "$lease:$ref"
story="$(gh issue view "$RALPH_ITERATION_ISSUE" --json number,title,labels,body,state)"
for call in --checkpoint --record-attempt; do
  rc=0
  printf '%s' "$story" | ralph "$call" - "out of context" >/dev/null 2>&1 || rc=$?
  echo "agent $call rc=$rc" >> "$RALPH_LOG"
done
""")
        proc = self.world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = self.world.log_lines()
        self.assertIn("slot=1", log, proc.stdout)
        for call in ("--checkpoint", "--record-attempt"):
            self.assertIn("agent %s rc=%d" % (call, ralph_claim.EXIT_CLAIM_LOST),
                          log, proc.stdout)
        # Neither wrote: the Handoff comment and the Attempt are both absent,
        # and the only comment left is this Slot's own token ledger.
        comments = [ln for ln in log if ln.startswith("gh issue comment")]
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("Token ledger", comments[0])
        self.assertNotIn("ralph:handoff", " ".join(log))
