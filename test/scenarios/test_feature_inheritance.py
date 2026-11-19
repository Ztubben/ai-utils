"""A Feature pull request inherits its Story pull requests' model reviews (#172).

PR #170 reached the base branch through an admin bypass: nothing produced
`ralph/model-review` on a Feature head. The Feature is not re-reviewed; the
completion pass vouches that every commit on the rebased Feature branch is the
unchanged counterpart of a reviewed Story's merged change, and only then writes
the context on the exact Feature head itself.

Each world runs a Feature Story through the real Loop first -- implemented,
reviewed and merged into its Feature branch -- with the PRD held back from
`state:ready`, so the test can change the world between the Story's merge and
the Feature's integration.
"""
import json
import os
import subprocess
import unittest

import harness
import invariants
import ralph_review

FEATURE = 'feature/10-greeting-feature'


class FeatureInheritance(unittest.TestCase):

    def world(self):
        spec = harness.load('afk_happy_path')
        spec.pop('matrix', None)
        story = spec['github']['issues'][0]
        story['body'] = story['body'].replace('Parent: None', 'Parent: #10')
        spec['github']['issues'].append(dict(
            number=10, title='Greeting feature', labels=['prd'],
            body='## Problem\nNo greeting.\n\nDepends on: None\n'))
        world = harness.World(spec)
        self.addCleanup(world.cleanup)
        world._watch = invariants.Watch(world)
        for _ in range(3):
            self.tick(world)
            if world.state()['issues']['1']['state'] == 'CLOSED':
                break
        state = world.state()
        self.assertEqual(state['issues']['1']['state'], 'CLOSED')
        pr, = state['pulls'].values()
        self.assertEqual((pr['state'], pr['baseRefName']), ('MERGED', FEATURE))
        world._story_pr = pr['number']
        return world

    def tick(self, world):
        proc = world.tick()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        harness.check_calls(world, 1, proc.stdout)
        self.assertEqual(world._watch.check(), [])
        return proc

    def edit(self, world, change):
        state = world.state()
        change(state)
        with open(world.state_path, 'w') as fh:
            json.dump(state, fh)

    def ready(self, world, review_on_every_head=False):
        def change(state):
            state['issues']['10']['labels'].append('state:ready')
            if review_on_every_head:
                # A producer that is not the Loop -- or an admin -- satisfying
                # the context on every head, the Feature's included.
                state['ci'].append(dict(context='ralph/model-review', state='SUCCESS'))
        self.edit(world, change)

    def push_to_feature(self, world, path, text, subject):
        clone = os.path.join(world.root, 'intruder')
        if not os.path.isdir(clone):
            harness._git(['clone', '-q', world.remote, clone], world.root, world.env)
        harness._git(['fetch', '-q', 'origin'], clone, world.env)
        harness._git(['checkout', '-q', '-B', FEATURE, 'origin/' + FEATURE], clone, world.env)
        with open(os.path.join(clone, path), 'w') as fh:
            fh.write(text)
        harness._git(['add', path], clone, world.env)
        harness._git(['commit', '-q', '-m', subject], clone, world.env)
        harness._git(['push', '-q', 'origin', 'HEAD:refs/heads/' + FEATURE], clone, world.env)
        return harness._git(['rev-parse', 'HEAD'], clone, world.env)

    def feature_pr(self, world):
        prs = [p for p in world.state()['pulls'].values() if p['headRefName'] == FEATURE]
        self.assertEqual(len(prs), 1, prs)
        return prs[0]

    def statuses_written_by_the_pass(self, world, before):
        """Every ralph/model-review status the Feature pass wrote, by head."""
        written = {}
        for head, entries in world.state()['statuses'].items():
            new = entries[len(before.get(head, [])):]
            new = [s for s in new if s['context'] == 'ralph/model-review']
            if new:
                written[head] = new
        return written

    # -- red first: the bypass #170 took ----------------------------------

    def test_an_unreviewed_commit_never_reaches_the_base(self):
        world = self.world()
        self.push_to_feature(world, 'smuggled.txt', 'nobody reviewed this\n',
                             'feat: smuggled past review')
        self.ready(world, review_on_every_head=True)
        self.tick(world)
        state = world.state()
        pr = self.feature_pr(world)
        self.assertEqual(pr['state'], 'OPEN', 'the Feature merged an unreviewed commit')
        prd = state['issues']['10']
        self.assertEqual(prd['state'], 'OPEN')
        self.assertIn('ready-for-human', prd['labels'])
        self.assertNotIn('needs-human', prd['labels'])
        body = prd['comments'][-1]['body']
        self.assertIn('feat: smuggled past review', body)
        # The commit is named as it is on the Feature head that was refused.
        head = harness._git(['rev-parse', 'refs/heads/' + FEATURE], world.remote, world.env)
        subject_sha = harness._git(['log', '-1', '--format=%h', head], world.remote, world.env)
        self.assertIn(subject_sha, body)

    # -- the inherited review ----------------------------------------------

    def test_a_feature_of_reviewed_stories_inherits_their_review(self):
        world = self.world()
        before = world.state()['statuses']
        self.ready(world)
        self.tick(world)
        state = world.state()
        pr = self.feature_pr(world)
        self.assertEqual(pr['state'], 'MERGED')
        self.assertEqual(state['issues']['10']['state'], 'CLOSED')
        self.assertNotIn('ready-for-human', state['issues']['10']['labels'])
        written = self.statuses_written_by_the_pass(world, before)
        # Exactly one write, on exactly the head that merged.
        self.assertEqual(list(written), [pr['mergedHeadOid']])
        status, = written[pr['mergedHeadOid']]
        self.assertEqual(status['state'], 'success')
        self.assertIn('#%d' % world._story_pr, status['description'])
        merges = [c['argv'] for c in world.calls() if c['tool'] == 'gh'
                  and c['argv'][:2] == ['pr', 'merge'] and FEATURE in c['argv']]
        self.assertEqual(len(merges), 1)
        self.assertNotIn('--admin', merges[0])

    def test_a_story_merged_without_a_review_blocks_the_feature(self):
        world = self.world()
        story_pr = world.state()['pulls'][str(world._story_pr)]
        self.edit(world, lambda state: state['statuses'].pop(story_pr['mergedHeadOid']))
        before = world.state()['statuses']
        self.ready(world)
        self.tick(world)
        state = world.state()
        self.assertEqual(self.feature_pr(world)['state'], 'OPEN')
        prd = state['issues']['10']
        self.assertIn('ready-for-human', prd['labels'])
        self.assertNotIn('needs-human', prd['labels'])
        self.assertIn('#1', prd['comments'][-1]['body'])
        self.assertIn('#%d' % world._story_pr, prd['comments'][-1]['body'])
        self.assertEqual(self.statuses_written_by_the_pass(world, before), {})

    def test_a_recorded_human_approval_of_the_story_head_is_inherited(self):
        world = self.world()
        story_pr = world.state()['pulls'][str(world._story_pr)]
        head = story_pr['mergedHeadOid']

        def change(state):
            state['statuses'].pop(head)
            state['issues']['1']['comments'].append(dict(
                id=777, node_id='IC_777', author='maintainer',
                createdAt='2026-01-01T00:10:00Z', url='https://example.invalid/c/777',
                body=ralph_review.arbitration_record(dict(
                    review='PRR_1', decision='APPROVED', head=head,
                    reviewer='maintainer', overrode=[], note='Approved'))))
        self.edit(world, change)
        self.ready(world)
        self.tick(world)
        self.assertEqual(self.feature_pr(world)['state'], 'MERGED')


if __name__ == '__main__':
    unittest.main()
