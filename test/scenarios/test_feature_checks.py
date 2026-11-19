"""Feature integration must wait for GitHub, even after a green local gate (#171)."""
import json
import unittest

import test_reconciliation_claims
import harness
import invariants


class FeatureChecks(unittest.TestCase):
    world = test_reconciliation_claims.ReconciliationClaims.world

    def tick(self, world):
        if not hasattr(world, '_feature_watch'):
            world._feature_watch = invariants.Watch(world)
        proc = world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        harness.check_calls(world, 1, proc.stdout)
        self.assertEqual(world._feature_watch.check(), [])
        return proc

    def set_ci(self, world, verdict=None, review=True):
        state = world.state()
        state['ci'] = ([] if verdict is None else [dict(
            name='test', status='IN_PROGRESS' if verdict == 'PENDING' else 'COMPLETED', conclusion=verdict)])
        if review:
            state['ci'].append(dict(context='ralph/model-review', state='SUCCESS'))
        with open(world.state_path, 'w') as fh:
            json.dump(state, fh)

    def test_failed_ci_blocks_feature_despite_green_local_gate(self):
        world = self.world(feature=True)
        self.set_ci(world, 'FAILURE')
        proc = self.tick(world)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        state = world.state()
        pr, = state['pulls'].values()
        self.assertEqual(pr['state'], 'OPEN')
        prd = state['issues']['10']
        self.assertIn('ready-for-human', prd['labels'])
        self.assertNotIn('needs-human', prd['labels'])
        self.assertIn('test', prd['comments'][-1]['body'])
        self.assertIn(pr['url'], prd['comments'][-1]['body'])

    def test_pending_then_failed_review_blocks_existing_pr(self):
        world = self.world(feature=True)
        self.set_ci(world, 'SUCCESS', review=False)
        self.tick(world)
        state = world.state()
        pr, = state['pulls'].values()
        number = pr['number']
        state['ci'].append(dict(context='ralph/model-review', state='FAILURE'))
        with open(world.state_path, 'w') as fh:
            json.dump(state, fh)
        self.tick(world)
        state = world.state()
        pr, = state['pulls'].values()
        self.assertEqual(pr['number'], number)
        self.assertEqual(pr['state'], 'OPEN')
        self.assertIn('ready-for-human', state['issues']['10']['labels'])
        self.assertIn('ralph/model-review', state['issues']['10']['comments'][-1]['body'])

    def test_missing_checks_wait_then_reuse_pr(self):
        for verdict, review in [(None, True), ('SUCCESS', False), ('PENDING', True)]:
            with self.subTest(verdict=verdict, review=review):
                world = self.world(feature=True)
                self.set_ci(world, verdict, review)
                self.tick(world)
                pr, = world.state()['pulls'].values()
                self.assertEqual(pr['state'], 'OPEN')
                self.assertNotIn('ready-for-human', world.state()['issues']['10']['labels'])
                self.set_ci(world, 'SUCCESS')
                proc = self.tick(world)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                pr, = world.state()['pulls'].values()
                self.assertEqual(pr['state'], 'MERGED')
                merges = [c['argv'] for c in world.calls() if c['tool'] == 'gh'
                          and c['argv'][:2] == ['pr', 'merge']]
                self.assertEqual(len(merges), 1)
                self.assertIn('--match-head-commit', merges[0])
                self.assertNotIn('--admin', merges[0])
