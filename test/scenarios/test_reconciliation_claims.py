"""Two Slots reconcile the same Feature or human decision (#140)."""
import json
import subprocess
import unittest

import harness
import invariants
import ralph_review


def seed_reviewed_story(world, story=1, feature='feature/10-greeting-feature'):
    """Story #1 already merged into its Feature through a reviewed pull request.

    The Feature pull request inherits that review (#172), so a Feature world
    needs the Story pull request it inherits from: its head reviewed, and the
    Feature branch carrying exactly its change.
    """
    def git(*args):
        return subprocess.run(['git'] + list(args), cwd=world.checkout, env=world.env,
                              capture_output=True, check=True, text=True).stdout.strip()
    base = git('rev-parse', 'HEAD')
    branch = 'ralph/%d-add-the-greeting' % story
    git('checkout', '-q', '-b', branch)
    with open('%s/greeting.txt' % world.checkout, 'w') as fh:
        fh.write('hello\n')
    git('add', 'greeting.txt')
    git('commit', '-q', '-m', 'feat: greeting')
    head = git('rev-parse', 'HEAD')
    with open(world.state_path) as fh:
        state = json.load(fh)
    number = state['next_number']
    # Squash-merged, as `afk_merge: squash` does: the Feature carries the
    # Story's change in a commit of its own, never the reviewed commit itself.
    squash = git('commit-tree', head + '^{tree}', '-p', base,
                 '-m', 'Add the greeting (#%d)' % number)
    git('push', '-q', 'origin', 'HEAD:refs/heads/' + branch,
        '%s:refs/heads/%s' % (squash, feature))
    git('checkout', '-q', '-')
    state['next_number'] += 1
    state['pulls'][str(number)] = dict(
        number=number, title='Add the greeting', state='MERGED',
        body='%s\n\nRefs #%d' % (ralph_review.MANAGED_PR_MARKER, story),
        headRefName=branch, baseRefName=feature, mergedHeadOid=head,
        mergedBaseOid=base, mergeCommit=squash, mergedAt='2026-01-01T00:00:00Z',
        url='https://github.com/acme/target/pull/%d' % number,
        comments=[], reviews=[], reviewComments=[])
    state['statuses'][head] = [dict(state='success', context='ralph/model-review',
                                    description='approve', at='2026-01-01T00:00:00Z')]
    with open(world.state_path, 'w') as fh:
        json.dump(state, fh)


class ReconciliationClaims(unittest.TestCase):
    def world(self, feature=False, blocker=False):
        spec = harness.load('afk_happy_path')
        spec.pop('matrix', None)
        spec['slots'] = ['1', '2']
        spec['config'].setdefault('concurrency', {})['max_slots'] = 2
        if feature:
            spec['github']['ci'].append(dict(context='ralph/model-review', state='SUCCESS'))
            spec['github']['issues'][0].update(state='CLOSED', body='Parent: #10\n')
            spec['github']['issues'].append(dict(number=10, title='Greeting feature',
                labels=['prd', 'state:ready'], body='A feature.'))
            if blocker:
                spec['config']['gating'][0]['run'] = 'false'
        else:
            spec['agents']['review'] = 'strict'
        world = harness.World(spec)
        self.addCleanup(world.cleanup)
        if feature:
            seed_reviewed_story(world)
        else:
            proc = world.tick()
            self.assertEqual(proc.returncode, 0, proc.stdout)
            state = world.state()
            self.assertIn('state:blocked', state['issues']['1']['labels'])
            pr, = state['pulls'].values()
            head = harness._git(['rev-parse', 'refs/heads/' + pr['headRefName']],
                                world.remote, world.env)
            pr['reviews'].append(dict(id=9001, node_id='PRR_9001', author='maintainer', state='APPROVED',
                body='Approved by the human', commit_id=head,
                submittedAt='2026-12-01T00:00:00Z'))
            with open(world.state_path, 'w') as fh:
                json.dump(state, fh)
        return world

    def race(self, feature=False, blocker=False):
        world = self.world(feature, blocker)
        watch = invariants.Watch(world)
        issue = 10 if feature else 1
        world.step(dict(slot='1', action='tick', pause='issue-view', issue=issue))
        proc = world.tick(slot='2')
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn('could not claim #%s' % issue, proc.stdout)
        self.assertEqual(watch.check(), [])
        proc = world.step(dict(slot='1', action='resume'))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(watch.check(), [])
        claims = harness._git(['for-each-ref', '--format=%(refname)', 'refs/ralph/claims/'],
                             world.remote, world.env)
        self.assertEqual(claims, '')
        return world

    def test_expired_reconciliation_claims_cannot_act(self):
        for feature in (False, True):
            with self.subTest(feature=feature):
                world = self.world(feature)
                issue = 10 if feature else 1
                watch = invariants.Watch(world)
                world.step(dict(slot='1', action='tick', pause='issue-view', issue=issue))
                world.step(dict(slot='1', action='expire'))
                before = world.state()
                proc = world.step(dict(slot='1', action='resume'))
                self.assertEqual(proc.returncode, 0, proc.stdout)
                self.assertEqual(world.state(), before)
                self.assertEqual(watch.check(), [])

    def test_a_prd_closed_since_the_scan_is_not_integrated_again(self):
        world = self.world(feature=True)
        world.step(dict(slot='1', action='tick', pause='issue-view', issue=10))
        state = world.state()
        state['issues']['10']['state'] = 'CLOSED'
        with open(world.state_path, 'w') as fh:
            json.dump(state, fh)
        proc = world.step(dict(slot='1', action='resume'))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([p for p in world.state()['pulls'].values()
                          if p['headRefName'].startswith('feature/')], [])
        self.assertEqual(invariants.Watch(world).check(), [])

    def test_feature_is_integrated_once(self):
        world = self.race(feature=True)
        self.assertEqual(world.state()['issues']['10']['state'], 'CLOSED')
        merges = [c for c in world.calls() if c['tool'] == 'gh' and c['argv'][:2] == ['pr', 'merge']]
        self.assertEqual(len(merges), 1)

    def test_blocker_releases_the_prd_claim(self):
        world = self.race(feature=True, blocker=True)
        self.assertIn('ready-for-human', world.state()['issues']['10']['labels'])

    def test_human_approval_is_acted_on_and_recorded_once(self):
        world = self.race()
        self.assertIn('state:in-review', world.state()['issues']['1']['labels'])
        records = ralph_review.arbitrations(world.state()['issues']['1']['comments'])
        self.assertEqual(len(records), 1)
