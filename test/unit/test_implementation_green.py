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

    def test_feature_story_pushes_feature_head_and_opens_review_pr(self):
        plan = ralph_implementation.implementation_green_plan(
            story(parent=42), prd=prd())
        self.assertTrue(plan.ok, plan.errors)
        self.assertIn("HEAD:feature/42-provider-neutral-review", flattened(plan))
        self.assertTrue(any(c[:3] == ["gh", "pr", "create"] for c in plan.commands))

    def test_refuses_main(self):
        plan = ralph_implementation.implementation_green_plan(story(), base="Main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("main" in e for e in plan.errors))


class CliImplementationGreen(unittest.TestCase):
    def _run(self, pr_list):
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
            fh.write("#!/usr/bin/env bash\necho \"git $*\" >> \"$RALPH_LOG\"\n")
        os.chmod(git, os.stat(git).st_mode | stat.S_IEXEC)
        env = dict(os.environ, PATH=temp.name + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log, RALPH_PR_LIST=json.dumps(pr_list))
        config = os.path.join(FIXTURES, "config", "valid", "full.yml")
        proc = subprocess.run(
            [RALPH, "--implementation-green", "-", config], cwd=REPO_ROOT,
            input=json.dumps(story(state="in-review" if pr_list else "in-progress")),
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        with open(log) as fh:
            calls = fh.read()
        return proc, calls

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
