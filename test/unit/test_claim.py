"""Story Claims (#136): pure plans and real git against a bare origin."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "lib"))
import ralph_claim
from test_frozen_tooling import GIT_ENV, SelfHostedWorld, _write_exec, ready_story

RALPH = os.path.join(ROOT, "bin", "ralph")
A = "a" * 40


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

    def test_existing_claim_is_refused_and_names_holder(self):
        lease = ralph_claim.Lease("another:1", 1700000600)
        plan = ralph_claim.claim_plan(136, A, lease)
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
