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

    def test_absent_review_is_inherited_never_waited_on(self):
        """#172 replaced #171's F-1 triage: the pass is the Feature's producer."""
        world = self.world(feature=True)
        self.set_ci(world, 'SUCCESS', review=False)
        self.tick(world)
        state = world.state()
        pr, = [p for p in state['pulls'].values() if p['headRefName'].startswith('feature/')]
        self.assertEqual(pr['state'], 'MERGED')
        self.assertNotIn('ready-for-human', state['issues']['10']['labels'])
        reviews = [s for s in state['statuses'].get(pr['mergedHeadOid'], [])
                   if s['context'] == 'ralph/model-review']
        self.assertEqual([s['state'] for s in reviews], ['success'])

    def test_failed_ci_blocks_feature_despite_green_local_gate(self):
        world = self.world(feature=True)
        self.set_ci(world, 'FAILURE')
        proc = self.tick(world)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        state = world.state()
        pr, = self.feature_prs(state)
        self.assertEqual(pr['state'], 'OPEN')
        prd = state['issues']['10']
        self.assertIn('ready-for-human', prd['labels'])
        self.assertNotIn('needs-human', prd['labels'])
        self.assertIn('test', prd['comments'][-1]['body'])
        self.assertIn(pr['url'], prd['comments'][-1]['body'])

    def test_pending_then_failed_review_blocks_existing_pr(self):
        world = self.world(feature=True)
        self.set_ci(world, None, review=False)
        self.tick(world)
        state = world.state()
        pr, = self.feature_prs(state)
        number = pr['number']
        state['ci'] = [dict(name='test', status='COMPLETED', conclusion='SUCCESS'),
                       dict(context='ralph/model-review', state='FAILURE')]
        with open(world.state_path, 'w') as fh:
            json.dump(state, fh)
        self.tick(world)
        state = world.state()
        pr, = self.feature_prs(state)
        self.assertEqual(pr['number'], number)
        self.assertEqual(pr['state'], 'OPEN')
        self.assertIn('ready-for-human', state['issues']['10']['labels'])
        self.assertIn('ralph/model-review', state['issues']['10']['comments'][-1]['body'])

    def test_missing_checks_wait_then_reuse_pr(self):
        for verdict, review in [(None, True), ('PENDING', True), ('PENDING', False)]:
            with self.subTest(verdict=verdict, review=review):
                world = self.world(feature=True)
                self.set_ci(world, verdict, review)
                self.tick(world)
                pr, = self.feature_prs(world.state())
                self.assertEqual(pr['state'], 'OPEN')
                self.assertNotIn('ready-for-human', world.state()['issues']['10']['labels'])
                self.set_ci(world, 'SUCCESS', review)
                proc = self.tick(world)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                pr, = self.feature_prs(world.state())
                self.assertEqual(pr['state'], 'MERGED')
                merges = [c['argv'] for c in world.calls() if c['tool'] == 'gh'
                          and c['argv'][:2] == ['pr', 'merge']]
                self.assertEqual(len(merges), 1)
                self.assertIn('--match-head-commit', merges[0])
                self.assertNotIn('--admin', merges[0])
                # The inherited review is written once, on the head that merged.
                writes = [c['argv'] for c in world.calls() if c['tool'] == 'gh'
                          and c['argv'][:1] == ['api'] and 'statuses/' in ' '.join(c['argv'])]
                self.assertEqual(len(writes), 0 if review else 1)
                if writes:
                    self.assertIn('repos/{owner}/{repo}/statuses/' + pr['mergedHeadOid'], writes[0])

    @staticmethod
    def feature_prs(state):
        return [p for p in state['pulls'].values() if p['headRefName'].startswith('feature/')]
