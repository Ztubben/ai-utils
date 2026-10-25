"""Deterministic shared-backlog races and the Claim invariant (#138)."""
import os
import subprocess
import unittest
from unittest import mock

import harness
import invariants


class SeveralSlots(unittest.TestCase):
    def world(self):
        spec = harness.load('two_slots_different_stories')
        world = harness.World(spec)
        self.addCleanup(world.cleanup)
        return world

    def test_two_slots_work_different_stories_while_the_first_is_paused(self):
        world = self.world()
        watch = invariants.Watch(world)
        self.assertEqual(watch.check(), [])
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        self.assertEqual(world.paused_story('1'), 1)
        self.assertEqual(watch.check(), [])
        proc = world.step({'slot': '2', 'action': 'tick'})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(world.state()['issues']['2']['state'], 'CLOSED')
        self.assertEqual(world.state()['issues']['1']['state'], 'OPEN')
        self.assertEqual(watch.check(), [])
        proc = world.step({'slot': '1', 'action': 'resume'})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(watch.check(final=True), [])
        calls = [c for c in world.calls() if c.get('phase') == 'iteration']
        self.assertEqual({(c['slot'], c['story']) for c in calls}, {('1', 1), ('2', 2)})
        pushes = [c for c in world.calls() if c['tool'] == 'git']
        self.assertTrue(pushes, 'real git pushes must be observed')
        self.assertTrue(all(c.get('claims') for c in pushes if c.get('issues')))

    def test_expire_changes_the_real_lease_without_waiting(self):
        world = self.world()
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        world.step({'slot': '1', 'action': 'expire'})
        env = world.slot_env('2')
        proc = subprocess.run([world.ralph, '--claimed-issues'], cwd=world.checkout,
                              env=env, capture_output=True, text=True, check=True)
        self.assertNotIn('1', proc.stdout.split())
        # Expiry is a control operation, not a simulated product mutation.
        self.assertEqual(invariants.Watch(world).check(), [])

    def test_expire_completes_with_inherited_stdin_held_open(self):
        world = self.world()
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        read_fd, write_fd = os.pipe()
        saved_stdin = os.dup(0)
        run = subprocess.run

        def bounded_run(args, **kwargs):
            # Keep the regression bounded even when mktree inherits the open
            # pipe. subprocess.run kills and reaps it on timeout; world.cleanup
            # also kills the paused Tick group, including its heartbeat.
            kwargs['timeout'] = 5
            return run(args, **kwargs)

        try:
            os.dup2(read_fd, 0)
            with mock.patch.object(harness.subprocess, 'run', bounded_run):
                world.step({'slot': '1', 'action': 'expire'})
        finally:
            os.dup2(saved_stdin, 0)
            os.close(saved_stdin)
            os.close(read_fd)
            os.close(write_fd)
        proc = subprocess.run([world.ralph, '--claimed-issues'], cwd=world.checkout,
                              env=world.slot_env('2'), capture_output=True,
                              text=True, check=True)
        self.assertNotIn('1', proc.stdout.split())
        self.assertEqual(invariants.Watch(world).check(), [])

    def test_unclaimed_github_write_names_the_slot_and_issue(self):
        world = self.world()
        subprocess.run([os.path.join(world.bin, 'gh'), 'issue', 'comment', '1',
                        '--body', 'deliberately unclaimed'], cwd=world.checkout,
                       env=world.slot_env('2'), check=True, capture_output=True)
        violation, = invariants.Watch(world).check()
        self.assertEqual(violation.invariant, 'claim-held-for-mutation')
        self.assertIn('Slot 2', violation.detail)
        self.assertIn('#1', violation.detail)

    def test_unclaimed_real_push_is_caught_even_when_branch_is_deleted_later(self):
        world = self.world()
        env = world.slot_env('2')
        for ref in ('HEAD:refs/heads/ralph/1-add-the-greeting',
                    ':refs/heads/ralph/1-add-the-greeting'):
            subprocess.run(['git', 'push', '-q', 'origin', ref], cwd=world.checkout,
                           env=env, check=True, capture_output=True)
        violations = invariants.Watch(world).check()
        self.assertEqual(len(violations), 2)
        self.assertTrue(all(v.invariant == 'claim-held-for-mutation' for v in violations))

    def test_a_failed_push_observer_fails_the_scenario(self):
        world = self.world()
        env = dict(world.slot_env('2'), SCENARIO_BRANCHING='malformed')
        # Git accepts the push even when its post-receive observer fails.
        subprocess.run(['git', 'push', '-q', 'origin',
                        'HEAD:refs/heads/ralph/1-add-the-greeting'],
                       cwd=world.checkout, env=env, check=True, capture_output=True)
        with self.assertRaises(harness.ScenarioFailure):
            harness.check_calls(world, 1, '')

    def test_unclaimed_pr_reviews_statuses_and_comment_edits_are_caught(self):
        world = self.world()
        branch = 'ralph/1-add-the-greeting'
        subprocess.run(['git', 'push', '-q', 'origin', 'HEAD:refs/heads/' + branch],
                       cwd=world.checkout, env=world.env, check=True, capture_output=True)
        for args in [('pr', 'create', '--base', 'main', '--head', branch,
                      '--title', 'Story', '--body', 'Refs #1'),
                     ('issue', 'comment', '1', '--body', 'original')]:
            proc = world.gh(*args)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        pr, = world.state()['pulls'].values()
        comment = world.state()['issues']['1']['comments'][0]['id']
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=world.checkout,
                              env=world.env, check=True, capture_output=True,
                              text=True).stdout.strip()
        prefix = 'repos/{owner}/{repo}/'
        calls = [
            ['pr', 'comment', str(pr['number']), '--body', 'unclaimed'],
            ['api', '--method', 'POST', prefix + 'pulls/%s/reviews' % pr['number'],
             '-f', 'body=unclaimed', '-f', 'event=COMMENT'],
            ['api', prefix + 'statuses/' + head, '--method=POST',
             '-f', 'state=success', '-f', 'context=test'],
            ['api', '--method', 'PATCH', prefix + 'issues/comments/%s' % comment,
             '-f', 'body=unclaimed'],
        ]
        for args in calls:
            proc = subprocess.run([os.path.join(world.bin, 'gh')] + args,
                                  cwd=world.checkout, env=world.slot_env('2'),
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
        violations = invariants.Watch(world).check()
        self.assertEqual(len(violations), len(calls))
        self.assertTrue(all(v.invariant == 'claim-held-for-mutation' for v in violations))

    def test_a_lease_held_by_another_slot_does_not_authorize_a_write(self):
        world = self.world()
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        subprocess.run([os.path.join(world.bin, 'gh'), 'issue', 'comment', '1',
                        '--body', 'wrong holder'], cwd=world.checkout,
                       env=world.slot_env('2'), check=True, capture_output=True)
        violation, = invariants.Watch(world).check()
        self.assertIn('Slot 2', violation.detail)
        self.assertIn('#1', violation.detail)

    def test_expired_holder_is_caught_at_write_time(self):
        world = self.world()
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        world.step({'slot': '1', 'action': 'expire'})
        subprocess.run([os.path.join(world.bin, 'gh'), 'issue', 'comment', '1',
                        '--body', 'stale holder'], cwd=world.checkout,
                       env=world.slot_env('1'), check=True, capture_output=True)
        violation, = invariants.Watch(world).check()
        self.assertIn('Slot 1', violation.detail)
        self.assertIn('#1', violation.detail)


class SlotsOnSeveralMachines(unittest.TestCase):
    """`machines` gives a Slot its own clone of the same remote (#139).

    The Slots of one machine are worktrees of one clone (ADR-0008), so git
    refuses the second one a branch the first has checked out. Releasing a
    Claim detaches, which keeps that out of the way of an orderly hand-over --
    but a *takeover* is the case where the previous holder never released and
    so never detached, and that is a cross-machine case by construction.
    """

    def world(self, **overrides):
        world = harness.World(dict(harness.load('two_slots_different_stories'),
                                   **overrides))
        self.addCleanup(world.cleanup)
        return world

    def git(self, checkout, *args, env):
        return subprocess.run(['git'] + list(args), cwd=checkout, env=env,
                              capture_output=True, text=True)

    def test_every_slot_shares_one_clone_unless_a_world_says_otherwise(self):
        world = self.world()
        self.assertEqual({world.slot_checkout(slot) for slot in world.slots},
                         {world.checkout})

    def test_a_slot_on_another_machine_works_from_its_own_clone(self):
        world = self.world(machines={'2': 'b'})
        self.assertEqual(world.slot_checkout('1'), world.checkout)
        other = world.slot_checkout('2')
        self.assertNotEqual(other, world.checkout)
        # Its own clone of the *same* remote: one backlog, one set of refs and
        # Claims. Its Slot worktree is a sibling of that clone, not of Slot 1's.
        url = self.git(other, 'remote', 'get-url', 'origin', env=world.env)
        self.assertEqual(url.stdout.strip(), world.remote, url.stderr)
        self.assertTrue(world.slot_env('2')['RALPH_SLOT_DIR'].startswith(other))

    def test_another_machine_is_not_refused_a_branch_slot_one_checked_out(self):
        branch = 'ralph/1-add-the-greeting'

        def take_over(world):
            """Each Slot puts the same Story branch in its own worktree."""
            self.git(world.checkout, 'push', '-q', 'origin', 'HEAD:refs/heads/' + branch,
                     env=world.env)
            results = []
            for slot in ('1', '2'):
                checkout = world.slot_checkout(slot)
                self.git(checkout, 'fetch', '-q', 'origin', env=world.env)
                results.append(self.git(
                    checkout, 'worktree', 'add', '-q',
                    world.slot_env(slot)['RALPH_SLOT_DIR'], '-B', branch,
                    'origin/' + branch, env=world.env))
            return results

        first, second = take_over(self.world())
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(second.returncode, 0)
        self.assertIn('already used by worktree', second.stderr)

        first, second = take_over(self.world(machines={'2': 'b'}))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)

    def test_machines_must_name_declared_slots(self):
        with self.assertRaises(harness.ScenarioFailure) as caught:
            self.world(machines={'3': 'b'})
        self.assertIn('machines name undeclared Slots: 3', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
