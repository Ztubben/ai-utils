"""Contract tests for implementation-green -> marked PR -> In Review (#49)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")
sys.path.insert(0, LIB_DIR)

import ralph_implementation  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402


def story(type_="afk", state="in-progress", number=49, parent=None):
    body = ("## Acceptance Criteria\n- [ ] reviewed\n\nParent: %s\n"
            "Depends on: None\n" % ("#42" if parent else "None"))
    if type_ == "hil":
        body += "\n## Bench Test Procedure\n- exercise it\n"
    return {
        "number": number, "title": "Review the implementation",
        "labels": [{"name": "type:" + type_}, {"name": "state:" + state}],
        "body": body, "state": "OPEN",
    }


def prd():
    return {"number": 42, "title": "Provider-neutral review",
            "labels": [{"name": "prd"}], "body": "PRD", "state": "OPEN"}


def flattened(plan):
    return [part for command in plan.commands for part in command]


class ImplementationGreenPlan(unittest.TestCase):
    def test_afk_opens_marked_pr_and_enters_review_without_completing(self):
        plan = ralph_implementation.implementation_green_plan(story("afk"))
        self.assertTrue(plan.ok, plan.errors)
        create = next(c for c in plan.commands if c[:3] == ["gh", "pr", "create"])
        self.assertIn(ralph_review.MANAGED_PR_MARKER, " ".join(create))
        edit = next(c for c in plan.commands if c[:3] == ["gh", "issue", "edit"])
        self.assertIn("state:in-review", edit)
        self.assertIn("state:in-progress", edit)
        tokens = flattened(plan)
        self.assertNotIn("merge", tokens)
        self.assertNotIn("close", tokens)

    def test_hil_uses_the_same_review_transition_without_awaiting_bench(self):
        plan = ralph_implementation.implementation_green_plan(story("hil"))
        self.assertTrue(plan.ok, plan.errors)
        self.assertIn("state:in-review", flattened(plan))
        self.assertNotIn("state:awaiting-bench", flattened(plan))
        self.assertFalse(any(c[:3] == ["gh", "pr", "merge"] for c in plan.commands))

    def test_second_green_iteration_updates_the_marked_open_pr(self):
        existing = {"number": 123, "body": ralph_review.MANAGED_PR_MARKER}
        plan = ralph_implementation.implementation_green_plan(
            story(state="in-review"), existing_pr=existing)
        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.updated)
        self.assertIn(["gh", "pr", "edit"], [c[:3] for c in plan.commands])
        self.assertFalse(any(c[:3] == ["gh", "pr", "create"] for c in plan.commands))
        self.assertFalse(any(c[:3] == ["gh", "issue", "edit"] for c in plan.commands))

    def test_unmarked_pr_is_outside_review_and_refused(self):
        unmarked = {"number": 123, "body": "A human pull request"}
        self.assertFalse(ralph_review.is_managed_pr(unmarked))
        plan = ralph_implementation.implementation_green_plan(
            story(state="in-review"), existing_pr=unmarked)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("not Ralph-managed" in e for e in plan.errors))

    def test_marker_match_is_exact_not_a_title_or_branch_heuristic(self):
        self.assertTrue(ralph_review.is_managed_pr(
            {"body": "prefix\n%s\nsuffix" % ralph_review.MANAGED_PR_MARKER}))
        self.assertFalse(ralph_review.is_managed_pr(
            {"body": "ralph-managed-pr:v1", "title": "Ralph Story"}))

    def test_review_candidates_drop_unmarked_prs_before_model_dispatch(self):
        marked = {"number": 1, "body": ralph_review.MANAGED_PR_MARKER}
        unmarked = {"number": 2, "body": "human-owned"}
        self.assertEqual(ralph_review.review_candidates([unmarked, marked]), [marked])

    def test_feature_story_pushes_its_own_story_branch(self):
        """PRD #69: a Feature story owns its branch, it does not share one."""
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), prd=prd())
        self.assertTrue(plan.ok, plan.errors)
        self.assertIn("HEAD:ralph/49-review-the-implementation", flattened(plan))
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in plan.commands))

    def test_feature_story_pull_request_targets_the_feature_branch(self):
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), base="develop", prd=prd())
        create = next(c for c in plan.commands if c[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1],
                         "feature/42-provider-neutral-review")
        self.assertEqual(plan.base, "feature/42-provider-neutral-review")
        self.assertNotIn("develop", create)

    def test_orphan_story_pull_request_targets_the_base_branch(self):
        plan = ralph_implementation.implementation_green_plan(
            story(), base="develop")
        create = next(c for c in plan.commands if c[:3] == ["gh", "pr", "create"])
        self.assertEqual(create[create.index("--base") + 1], "develop")
        self.assertEqual(plan.base, "develop")
        self.assertIsNone(plan.feature)
        self.assertFalse(plan.created_feature)

    def test_absent_feature_branch_is_created_off_base_before_anything_else(self):
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), base="develop", prd=prd(), feature_exists=False)
        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.created_feature)
        self.assertEqual(
            plan.commands[:2],
            [["git", "fetch", "origin", "develop"],
             ["git", "push", "origin",
              "origin/develop:refs/heads/feature/42-provider-neutral-review"]])

    def test_existing_feature_branch_is_not_recreated(self):
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), base="develop", prd=prd(), feature_exists=True)
        self.assertFalse(plan.created_feature)
        self.assertEqual(plan.commands[0][:3], ["git", "push", "-u"])

    def test_an_orphan_story_never_creates_a_feature_branch(self):
        plan = ralph_implementation.implementation_green_plan(
            story(), base="develop", feature_exists=False)
        self.assertFalse(plan.created_feature)
        self.assertNotIn("refs/heads/feature/42-provider-neutral-review",
                         flattened(plan))

    def test_two_stories_of_one_feature_open_two_pull_requests(self):
        """The pull request is the Story, so a sibling never reuses one."""
        first = ralph_implementation.implementation_green_plan(
            story(number=49, parent=42), base="develop", prd=prd())
        second = ralph_implementation.implementation_green_plan(
            story(number=50, parent=42), base="develop", prd=prd())
        self.assertNotEqual(first.branch, second.branch)
        for plan in (first, second):
            create = next(c for c in plan.commands
                          if c[:3] == ["gh", "pr", "create"])
            self.assertEqual(create[create.index("--base") + 1],
                             "feature/42-provider-neutral-review")
            self.assertEqual(create[create.index("--head") + 1], plan.branch)
        self.assertIn("Refs #49", " ".join(flattened(first)))
        self.assertIn("Refs #50", " ".join(flattened(second)))

    def test_every_story_pull_request_carries_the_managed_marker(self):
        for kwargs in ({}, {"parent": 42}):
            plan = ralph_implementation.implementation_green_plan(
                story(**kwargs), prd=prd() if kwargs else None)
            create = next(c for c in plan.commands
                          if c[:3] == ["gh", "pr", "create"])
            self.assertIn(ralph_review.MANAGED_PR_MARKER, " ".join(create))
            self.assertIn(story(**kwargs)["title"], create)

    def test_feature_story_still_neither_merges_nor_closes(self):
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), prd=prd(), feature_exists=False)
        tokens = flattened(plan)
        self.assertNotIn("merge", tokens)
        self.assertNotIn("close", tokens)
        self.assertNotIn("state:awaiting-bench", tokens)

    def test_review_bundle_for_a_feature_story_is_only_that_story(self):
        """The reviewed diff is the pull request's own base..head range.

        With one pull request per Story targeting the Feature branch, that
        range is the Story's change alone -- a sibling's commits are behind
        the base and never enter the bundle.
        """
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), base="develop", prd=prd())
        self.assertEqual(plan.base, "feature/42-provider-neutral-review")

        calls = []

        def fake_git(args, root):
            calls.append(list(args))
            return args[1] + "\n" if args[0] == "rev-parse" else ""

        original = ralph_review_context._git
        ralph_review_context._git = fake_git
        self.addCleanup(setattr, ralph_review_context, "_git", original)
        # The pull request's base is the Feature branch tip, so this is the
        # range the reviewer is handed -- never the Feature's whole history.
        ralph_review_context.head_diff(
            {"baseRefOid": "featuretip", "headRefOid": "storyhead"}, ".")
        self.assertIn(["diff", "--no-ext-diff", "featuretip", "storyhead"],
                      calls)

    def test_refuses_main(self):
        plan = ralph_implementation.implementation_green_plan(story(), base="Main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("main" in e for e in plan.errors))


class CliImplementationGreen(unittest.TestCase):
    def _run(self, pr_list, story_json=None, prd_json=None,
             feature_on_remote=True):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log = os.path.join(temp.name, "calls.log")
        mock = os.path.join(temp.name, "gh")
        with open(mock, "w") as fh:
            fh.write("#!/usr/bin/env bash\n"
                     "echo \"gh $*\" >> \"$RALPH_LOG\"\n"
                     "if [[ \"$1 $2\" == \"pr list\" ]]; then "
                     "printf '%s\\n' \"$RALPH_PR_LIST\"; fi\n")
        os.chmod(mock, os.stat(mock).st_mode | stat.S_IEXEC)
        git = os.path.join(temp.name, "git")
        with open(git, "w") as fh:
            # `ls-remote --exit-code` is how the CLI asks whether the Feature
            # branch is already on origin; everything else just logs.
            fh.write("#!/usr/bin/env bash\n"
                     "echo \"git $*\" >> \"$RALPH_LOG\"\n"
                     "if [[ \"$1\" == \"ls-remote\" ]]; then "
                     "exit \"$RALPH_LS_REMOTE_RC\"; fi\n")
        os.chmod(git, os.stat(git).st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=temp.name + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log, RALPH_PR_LIST=json.dumps(pr_list),
                   RALPH_LS_REMOTE_RC="0" if feature_on_remote else "2")
        config = os.path.join(FIXTURES, "config", "valid", "full.yml")
        args = [RALPH, "--implementation-green", "-", config]
        if prd_json is not None:
            prd_path = os.path.join(temp.name, "prd.json")
            with open(prd_path, "w") as fh:
                json.dump(prd_json, fh)
            args.append(prd_path)
        if story_json is None:
            story_json = story(state="in-review" if pr_list else "in-progress")
        proc = subprocess.run(
            args, cwd=REPO_ROOT, input=json.dumps(story_json),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with open(log) as fh:
            calls = fh.read()
        return proc, calls

    def test_cli_creates_the_feature_branch_when_the_remote_lacks_it(self):
        proc, calls = self._run([], story_json=story(parent=42), prd_json=prd(),
                                feature_on_remote=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("git ls-remote --exit-code --heads origin "
                      "feature/42-provider-neutral-review", calls)
        self.assertIn("git push origin origin/develop:refs/heads/"
                      "feature/42-provider-neutral-review", calls)
        self.assertIn("--base feature/42-provider-neutral-review", calls)

    def test_cli_reuses_an_existing_feature_branch(self):
        proc, calls = self._run([], story_json=story(parent=42), prd_json=prd(),
                                feature_on_remote=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("refs/heads/feature/42-provider-neutral-review", calls)
        self.assertIn("--base feature/42-provider-neutral-review", calls)

    def test_cli_never_asks_the_remote_about_an_orphan_story(self):
        proc, calls = self._run([])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("ls-remote", calls)
        self.assertIn("--base develop", calls)

    def test_cli_discovers_and_updates_existing_marked_pr(self):
        proc, calls = self._run([
            {"number": 88, "body": ralph_review.MANAGED_PR_MARKER}])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pr edit 88", calls)
        self.assertNotIn("pr create", calls)

    def test_cli_refuses_existing_unmarked_pr_without_push_or_model_work(self):
        proc, calls = self._run([{"number": 88, "body": "human PR"}])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("not Ralph-managed", proc.stderr)
        self.assertNotIn("git push", calls)
        self.assertNotIn("pr edit", calls)


if __name__ == "__main__":
    unittest.main()
