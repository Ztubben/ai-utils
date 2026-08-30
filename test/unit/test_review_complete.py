"""Contract tests for review-gated completion (#59).

The end of the loop. Once the current head's CI is green and the model-review
gate is satisfied -- by an approving review or by a human's approval -- a Story
merges its own pull request into its own base: the base branch for an Orphan
Story, the Feature integration branch for a Feature Story. An AFK Story closes
there as Passing. A HIL Feature Story merges too and then parks at Awaiting
Bench Verification, because model review never replaces physical verification
and the gate that enforces that now stands at the Feature boundary. A HIL
Orphan Story has no Feature branch to merge into and is not merged at all.
Neither path ever targets main.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_review  # noqa: E402
import ralph_review_complete  # noqa: E402
import ralph_review_render  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def story(number=59, type_="afk", state="in-review", parent=None):
    body = "## Acceptance Criteria\n\n- [ ] it merges\n\nParent: %s\nDepends on: None\n" % (
        "#%d" % parent if parent else "None")
    if type_ == "hil":
        body += "\n## Bench Test Procedure\n- poke it\n"
    return {"number": number, "title": "Completion", "body": body, "state": "OPEN",
            "labels": [{"name": "state:" + state}, {"name": "type:" + type_},
                       {"name": "prio:1"}]}


def prd(number=42):
    return {"number": number, "title": "PRD: model review", "state": "OPEN",
            "labels": [{"name": "prd"}, {"name": "state:ready"}],
            "body": "## What to build\nreview\n\nParent: None\nDepends on: None\n"}


def check(name="test", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": "COMPLETED",
            "conclusion": conclusion}


def status(context=ralph_review_render.CHECK_CONTEXT, state="SUCCESS"):
    return {"__typename": "StatusContext", "context": context, "state": state}


def pull_request(checks=None, head=HEAD):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": "OPEN", "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": [], "comments": [],
            "statusCheckRollup": [check(), status()] if checks is None else checks}


def human_approval(head=HEAD):
    return [{"body": ralph_review.arbitration_record(
        {"review": "R-1", "decision": "APPROVED", "reviewer": "carl",
         "head": head, "overrode": ["F-1"]})}]


class TheGate(unittest.TestCase):
    """Both halves, read off the checks the current head actually carries."""

    def test_green_ci_and_an_approving_review_open_the_gate(self):
        gate = ralph_review_complete.gate_for(pull_request(), [])

        self.assertTrue(gate.ok, gate.errors)

    def test_a_failing_review_check_holds_the_gate_shut(self):
        gate = ralph_review_complete.gate_for(
            pull_request(checks=[check(), status(state="FAILURE")]), [])

        self.assertFalse(gate.ok)
        self.assertIn(ralph_review_render.CHECK_CONTEXT, " ".join(gate.errors))

    def test_red_ci_holds_the_gate_shut_even_with_review_satisfied(self):
        gate = ralph_review_complete.gate_for(
            pull_request(checks=[check(conclusion="FAILURE"), status()]), [])

        self.assertFalse(gate.ok)
        self.assertIn("test", " ".join(gate.errors))

    def test_pending_ci_is_not_green_yet(self):
        pending = {"__typename": "CheckRun", "name": "test",
                   "status": "IN_PROGRESS", "conclusion": None}
        gate = ralph_review_complete.gate_for(
            pull_request(checks=[pending, status()]), [])

        self.assertFalse(gate.ok)

    def test_no_review_check_at_all_is_not_a_satisfied_gate(self):
        gate = ralph_review_complete.gate_for(pull_request(checks=[check()]), [])

        self.assertFalse(gate.ok)

    def test_a_human_approval_of_this_head_satisfies_the_review_half(self):
        # AC (#59): the gate is satisfied by an approving review *or* by human
        # approval -- a human's decision stands on its own.
        gate = ralph_review_complete.gate_for(
            pull_request(checks=[check()]), human_approval())

        self.assertTrue(gate.ok, gate.errors)

    def test_a_human_approval_of_an_earlier_head_does_not_carry_over(self):
        gate = ralph_review_complete.gate_for(
            pull_request(checks=[check()]), human_approval(head="0" * 40))

        self.assertFalse(gate.ok)


class CompletingAnAfkStory(unittest.TestCase):
    def plan(self, **kwargs):
        kwargs.setdefault("story", story())
        kwargs.setdefault("pull_request", pull_request())
        kwargs.setdefault("comments", [])
        return ralph_review_complete.completion_plan(**kwargs)

    def flat(self, **kwargs):
        return [" ".join(c) for c in self.plan(**kwargs).commands]

    def test_an_orphan_story_squash_merges_and_closes_as_passing(self):
        plan = self.plan()

        self.assertTrue(plan.ok, plan.errors)
        flat = [" ".join(c) for c in plan.commands]
        merge = next(c for c in flat if "pr merge" in c)
        self.assertIn("--squash", merge)
        self.assertIn("70", merge)
        self.assertTrue(any(c.startswith("gh issue close 59") for c in flat), flat)

    def test_the_pull_request_keeps_its_history_while_base_gets_one_commit(self):
        # A squash is the whole point: the audit trail stays on the pull
        # request, and the base branch receives one clean commit.
        flat = self.flat()

        self.assertFalse(any("--merge" in c for c in flat), flat)
        self.assertFalse(any("git push --force" in c for c in flat), flat)

    def test_a_shut_gate_completes_nothing_at_all(self):
        plan = self.plan(pull_request=pull_request(
            checks=[check(), status(state="FAILURE")]))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_red_ci_completes_nothing_at_all(self):
        plan = self.plan(pull_request=pull_request(
            checks=[check(conclusion="FAILURE"), status()]))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_an_orphan_story_merges_into_the_base_branch(self):
        plan = self.plan(base="develop")

        self.assertEqual(plan.base, "develop")
        self.assertTrue(plan.merged)

    def test_a_feature_story_merges_into_its_feature_branch_and_closes(self):
        # PRD #69: the Story is the unit of the pull request, so a Feature
        # Story completes exactly as an Orphan Story does, one base along.
        plan = self.plan(story=story(parent=42), prd=prd(), base="develop")

        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.merged)
        self.assertEqual(plan.base, "feature/42-prd-model-review")
        flat = [" ".join(c) for c in plan.commands]
        self.assertTrue(any("pr merge 70" in c for c in flat), flat)
        self.assertTrue(any(c.startswith("gh issue close 59") for c in flat), flat)

    def test_a_feature_story_says_where_its_code_reaches_the_base_branch(self):
        flat = self.flat(story=story(parent=42), prd=prd(), base="develop")
        close = next(c for c in flat if c.startswith("gh issue close"))

        self.assertIn("feature/42-prd-model-review", close)
        self.assertIn("Feature #42", close)

    def test_the_merge_strategy_follows_afk_merge_whatever_the_base(self):
        for method, flag in (("merge", "--merge"), ("squash", "--squash"),
                             ("rebase", "--rebase")):
            for kwargs in ({}, {"story": story(parent=42), "prd": prd()}):
                flat = self.flat(afk_merge=method, **kwargs)
                merge = next(c for c in flat if "pr merge" in c)
                self.assertIn(flag, merge)

    def test_a_finished_story_branch_is_deleted_on_merge(self):
        for kwargs in ({}, {"story": story(parent=42), "prd": prd()}):
            merge = next(c for c in self.flat(**kwargs) if "pr merge" in c)
            self.assertIn("--delete-branch", merge)

    def test_a_feature_story_without_its_prd_is_refused(self):
        plan = self.plan(story=story(parent=42))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_it_never_merges_into_main(self):
        plan = self.plan(base="main")

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertIn("main", " ".join(plan.errors))

    def test_a_story_that_is_not_in_review_is_refused(self):
        plan = self.plan(story=story(state="in-progress"))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_an_unmarked_pull_request_is_outside_automated_completion(self):
        plan = self.plan(pull_request=dict(pull_request(), body="a human PR"))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class CompletingAHilStory(unittest.TestCase):
    def plan(self, **kwargs):
        kwargs.setdefault("story", story(type_="hil"))
        kwargs.setdefault("pull_request", pull_request())
        kwargs.setdefault("comments", [])
        return ralph_review_complete.completion_plan(**kwargs)

    def flat(self, **kwargs):
        return [" ".join(c) for c in self.plan(**kwargs).commands]

    def feature(self, **kwargs):
        kwargs.setdefault("story", story(type_="hil", parent=42))
        kwargs.setdefault("prd", prd())
        kwargs.setdefault("base", "develop")
        return self.plan(**kwargs)

    def test_an_orphan_story_parks_at_awaiting_bench_and_stays_open(self):
        plan = self.plan()

        self.assertTrue(plan.ok, plan.errors)
        flat = [" ".join(c) for c in plan.commands]
        label = next(c for c in flat if c.startswith("gh issue edit"))
        self.assertIn("--add-label state:awaiting-bench", label)
        self.assertIn("--remove-label state:in-review", label)
        self.assertFalse(any("issue close" in c for c in flat), flat)
        self.assertFalse(any("pr merge" in c for c in flat), flat)
        self.assertFalse(plan.merged)

    def test_the_bench_anchor_names_the_verified_commit(self):
        # The human bench-verifies at a commit, never at a moving branch tip.
        flat = [" ".join(c) for c in self.plan().commands]

        self.assertTrue(any(HEAD in c for c in flat), flat)

    def test_a_feature_story_merges_then_parks_for_the_bench(self):
        # PRD #69: its successors in the Feature build on its code, so it
        # lands on the Feature branch before the bench session happens.
        plan = self.feature()

        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.merged)
        self.assertTrue(plan.parked)
        self.assertEqual(plan.base, "feature/42-prd-model-review")
        flat = [" ".join(c) for c in plan.commands]
        self.assertTrue(any("pr merge 70" in c for c in flat), flat)
        label = next(c for c in flat if c.startswith("gh issue edit"))
        self.assertIn("--add-label state:awaiting-bench", label)
        self.assertFalse(any("issue close" in c for c in flat), flat)

    def test_a_merged_feature_storys_anchor_is_still_the_reviewed_commit(self):
        flat = self.flat(story=story(type_="hil", parent=42), prd=prd())
        anchor = next(c for c in flat if "Bench anchor" in c)

        self.assertIn(HEAD, anchor)
        # The branch is deleted on merge, so the anchor says how to reach the
        # commit the physical evidence is bound to.
        self.assertIn("refs/pull/70/head", anchor)

    def test_it_merges_before_it_parks(self):
        flat = self.flat(story=story(type_="hil", parent=42), prd=prd())
        merge = next(i for i, c in enumerate(flat) if "pr merge" in c)
        label = next(i for i, c in enumerate(flat)
                     if c.startswith("gh issue edit"))

        self.assertLess(merge, label)

    def test_model_review_never_replaces_the_bench(self):
        for plan in (self.plan(pull_request=pull_request(
                        checks=[check(), status()])),
                     self.feature()):
            flat = [" ".join(c) for c in plan.commands]
            self.assertFalse(any("issue close" in c for c in flat), flat)
            self.assertTrue(any("state:awaiting-bench" in c for c in flat), flat)

    def test_it_never_targets_main(self):
        plan = self.plan(base="main")

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class CliCompleteStory(unittest.TestCase):
    """Executed against fake provider, gh and git binaries on PATH (AC #59)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log = os.path.join(self.root, "calls.log")
        self._write(".ralph.yml", "version: 1\ngating:\n  - name: test\n"
                                  "    run: 'true'\nbranching:\n  base: develop\n"
                                  "notify:\n  github: someone\n")
        self._write("pr.json", json.dumps(pull_request()))
        self._write("issue.json", json.dumps({"comments": []}))
        for name in ("gh", "git"):
            path = self._write(name, "")
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s $*" >> "$RALPH_LOG"\n'
                         'if [[ "$1 $2" == "issue view" ]]; then\n'
                         '  cat "%s/issue.json"\n'
                         'fi\n'
                         'exit 0\n' % (name, self.root))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def run_cli(self, s, extra=()):
        self._write("story.json", json.dumps(s))
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log)
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--complete-story",
             os.path.join(self.root, "story.json"), ".ralph.yml", self.root,
             "--pr", os.path.join(self.root, "pr.json")] + list(extra),
            cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_the_whole_afk_flow_merges_and_closes(self):
        proc, calls = self.run_cli(story())

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gh pr merge 70 --squash", calls)
        self.assertIn("gh issue close 59", calls)

    def test_the_whole_hil_flow_parks_and_merges_nothing(self):
        proc, calls = self.run_cli(story(type_="hil"))

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("state:awaiting-bench", calls)
        self.assertNotIn("pr merge", calls)
        self.assertNotIn("issue close", calls)

    def test_a_shut_gate_reaches_github_not_at_all(self):
        self._write("pr.json", json.dumps(pull_request(
            checks=[check(), status(state="FAILURE")])))

        proc, calls = self.run_cli(story())

        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("pr merge", calls)
        self.assertNotIn("issue close", calls)


if __name__ == "__main__":
    unittest.main()
