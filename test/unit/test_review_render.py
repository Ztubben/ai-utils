"""Contract tests for rendering a validated review onto a pull request (#52).

The wrapper, not the model, writes to GitHub: these tests assert the ordered
command plan and the payload it hands `gh`, never how either is assembled.
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
import ralph_review_render  # noqa: E402

FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "reviews")
HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


class ReviewBody(unittest.TestCase):
    def test_states_the_reviewing_model_round_and_reviewed_commit(self):
        body = ralph_review_render.review_body(fixture("valid-inline.json"))
        self.assertIn("claude-opus-5", body)
        self.assertIn("round 1", body.lower())
        self.assertIn(HEAD, body)
        self.assertIn("rejection does not name the offending fields", body)

    def test_a_cross_cutting_finding_renders_in_the_summary(self):
        body = ralph_review_render.review_body(fixture("cross-cutting.json"))
        self.assertIn("F-3", body)
        self.assertIn("No test exercises an oversized payload", body)
        self.assertIn("missing_tests", body)


class ReviewPayload(unittest.TestCase):
    def test_located_findings_become_inline_comments_on_the_reviewed_commit(self):
        payload = ralph_review_render.review_payload(fixture("valid-inline.json"))
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertEqual([c["path"] for c in payload["comments"]],
                         ["lib/ralph_review_result.py"] * 2)
        first, second = payload["comments"]
        # F-1 spans lines 12-15; GitHub anchors a range at its last line.
        self.assertEqual((first["start_line"], first["line"], first["side"]),
                         (12, 15, "RIGHT"))
        self.assertIn("F-1", first["body"])
        # F-2 is a single line, so it carries no range at all.
        self.assertEqual(second["line"], 15)
        self.assertNotIn("start_line", second)


def pull_request(head=HEAD):
    return {"number": 70, "headRefOid": head,
            "body": ralph_review.MANAGED_PR_MARKER}


class RenderPlan(unittest.TestCase):
    def test_posts_the_review_then_updates_the_one_required_check(self):
        plan = ralph_review_render.render_plan(
            fixture("valid-inline.json"), pull_request(), "/tmp/review.json")
        self.assertTrue(plan.ok, plan.errors)
        post, check = plan.commands
        self.assertEqual(post, [
            "gh", "api", "--method", "POST",
            "repos/{owner}/{repo}/pulls/70/reviews", "--input", "/tmp/review.json"])
        self.assertIn("repos/{owner}/{repo}/statuses/" + HEAD, check)
        self.assertIn("context=" + ralph_review_render.CHECK_CONTEXT, check)
        self.assertIn("state=failure", check)

    def test_the_check_states_the_verdict_round_and_model_it_came_from(self):
        blocking = ralph_review_render.check_command(fixture("valid-inline.json"))
        description = next(a for a in blocking if a.startswith("description="))
        self.assertIn("round 1", description.lower())
        self.assertIn("claude-opus-5", description)
        self.assertIn("1 blocking", description)

        approving = ralph_review_render.check_command(fixture("non-blocking.json"))
        self.assertIn("state=success", approving)
        self.assertIn("context=" + ralph_review_render.CHECK_CONTEXT, approving)

    def test_a_result_for_a_stale_head_renders_nothing_at_all(self):
        moved = "0" * 40
        plan = ralph_review_render.render_plan(
            fixture("valid-inline.json"), pull_request(head=moved), "/tmp/review.json")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        reason = " ".join(plan.errors)
        self.assertIn("head", reason)
        self.assertIn(HEAD, reason)
        self.assertIn(moved, reason)

    def test_an_unmarked_pull_request_is_outside_automated_review(self):
        pr = dict(pull_request(), body="a human pull request")
        plan = ralph_review_render.render_plan(
            fixture("valid-inline.json"), pr, "/tmp/review.json")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class CliRenderReview(unittest.TestCase):
    """Executed against a fake `gh` that logs its argv and any --input file."""

    def run_cli(self, review, pr, diff=None, gh_exit=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log = os.path.join(temp.name, "calls.log")
        mock = os.path.join(temp.name, "gh")
        with open(mock, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\n'
                     'prev=""\n'
                     'for arg in "$@"; do\n'
                     '  if [[ "$prev" == "--input" ]]; then cat "$arg" >> "$RALPH_LOG"; fi\n'
                     '  prev="$arg"\n'
                     'done\n'
                     'exit %d\n' % gh_exit)
        os.chmod(mock, os.stat(mock).st_mode | stat.S_IEXEC)
        paths = []
        for name, data in (("review.json", review), ("pr.json", pr)):
            path = os.path.join(temp.name, name)
            with open(path, "w") as fh:
                json.dump(data, fh)
            paths.append(path)
        if diff is not None:
            paths.append(os.path.join(FIXTURES, "head.diff"))
        env = dict(os.environ, PATH=temp.name + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--render-review"] + paths,
            cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(log):
            with open(log) as fh:
                calls = fh.read()
        return proc, calls

    def test_posts_the_review_with_its_inline_threads_and_the_check(self):
        proc, calls = self.run_cli(fixture("valid-inline.json"), pull_request())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("repos/{owner}/{repo}/pulls/70/reviews", calls)
        self.assertIn('"commit_id": "%s"' % HEAD, calls)
        self.assertIn("F-1", calls)
        self.assertIn("statuses/" + HEAD, calls)
        self.assertIn("context=" + ralph_review_render.CHECK_CONTEXT, calls)

    def test_a_stale_head_reaches_github_not_at_all(self):
        proc, calls = self.run_cli(fixture("valid-inline.json"),
                                   pull_request(head="0" * 40))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(calls, "")
        self.assertIn("stale", proc.stderr)

    def test_a_result_that_fails_the_contract_is_never_rendered(self):
        proc, calls = self.run_cli(fixture("malformed-location.json"),
                                   pull_request(), diff=True)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(calls, "")
        self.assertIn("findings/0/location/line", proc.stderr)

    def test_a_failing_gh_call_is_reported_as_infrastructure(self):
        proc, calls = self.run_cli(fixture("cross-cutting.json"), pull_request(),
                                   gh_exit=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("FAILED", proc.stderr)
        self.assertIn("reviews", calls)


if __name__ == "__main__":
    unittest.main()
