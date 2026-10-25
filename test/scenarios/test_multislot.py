"""Deterministic shared-backlog races and the Claim invariant (#138)."""
import os
import subprocess
import unittest

import harness
import invariants


class SeveralSlots(unittest.TestCase):
    def test_operator_branch_is_skipped_without_mutation_then_resumes(self):
        for state in ('state:ready', 'state:in-progress'):
            with self.subTest(state=state):
                spec = harness.load('two_slots_different_stories')
                spec['github']['issues'][0]['labels'] = ['type:afk', 'prio:1', state]
                world = harness.World(spec)
                self.addCleanup(world.cleanup)
                branch = 'ralph/1-add-the-greeting'
                subprocess.run(['git', 'checkout', '-q', '-b', branch],
                               cwd=world.checkout, env=world.env, check=True)
                before = world.state()['issues']['1']
                watch = invariants.Watch(world)
                proc = world.tick()
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertIn('#1', proc.stdout)
                self.assertIn(world.checkout, proc.stdout)
                self.assertIn('skipping', proc.stdout)
                self.assertEqual(world.state()['issues']['1'], before)
                self.assertEqual(world.state()['issues']['2']['state'], 'CLOSED')
                self.assertEqual(watch.check(), [])
                refs = subprocess.run(['git', 'ls-remote', 'origin',
                                       'refs/ralph/claims/*'], cwd=world.checkout,
                                      env=world.env, check=True, capture_output=True,
                                      text=True).stdout
                self.assertEqual(refs, '')
                self.assertIn('released Claim for #1', proc.stdout)
                self.assertFalse(any(c.get('story') == 1 and c.get('phase')
                                     for c in world.calls()))
                subprocess.run(['git', 'checkout', '-q', '--detach'],
                               cwd=world.checkout, env=world.env, check=True)
                proc = world.tick()
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(world.state()['issues']['1']['state'], 'CLOSED')
                self.assertEqual(watch.check(final=True), [])

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

class OneSlotPerFeature(unittest.TestCase):
    """#141: sibling Feature Stories never run at once; an Orphan Story does."""

    def test_siblings_never_run_at_once_while_the_orphan_runs_alongside(self):
        world = harness.World(harness.load('two_slots_one_feature'))
        self.addCleanup(world.cleanup)
        watch = invariants.Watch(world)
        world.step({'slot': '1', 'action': 'tick', 'pause': 'after-claim'})
        self.assertEqual(world.paused_story('1'), 1)
        # Slot 1's unit holds the Story's and the Feature's Claim.
        claims = subprocess.run(['git', 'ls-remote', 'origin', 'refs/ralph/claims/*'],
                                cwd=world.checkout, env=world.env, check=True,
                                capture_output=True, text=True).stdout
        self.assertEqual(sorted(line.split()[1] for line in claims.splitlines()),
                         ['refs/ralph/claims/1', 'refs/ralph/claims/10'])
        proc = world.step({'slot': '2', 'action': 'tick'})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        issues = world.state()['issues']
        self.assertEqual(issues['3']['state'], 'CLOSED')
        self.assertIn('state:ready', issues['2']['labels'])
        self.assertEqual(watch.check(), [])
        proc = world.step({'slot': '1', 'action': 'resume'})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(watch.check(final=True), [])
        issues = world.state()['issues']
        self.assertEqual([issues[n]['state'] for n in '123'], ['CLOSED'] * 3)
        calls = [(c['slot'], c['story']) for c in world.calls()
                 if c.get('phase') == 'iteration']
        self.assertEqual(sorted(set(calls)), [('1', 1), ('1', 2), ('2', 3)])
        claims = subprocess.run(['git', 'ls-remote', 'origin', 'refs/ralph/claims/*'],
                                cwd=world.checkout, env=world.env, check=True,
                                capture_output=True, text=True).stdout
        self.assertEqual(claims, '')


if __name__ == '__main__':
    unittest.main()
