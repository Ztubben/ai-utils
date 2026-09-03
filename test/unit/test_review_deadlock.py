"""Contract tests for deadlocked negotiation (#57).

Two rounds that settle nothing end automated negotiation for one Story: it
moves to state:blocked, a native GitHub review is requested from the configured
human, and the pull request carries a summary of every unsettled finding with
both sides' arguments. Nothing here halts the loop -- unrelated Stories keep
running, and the global halt stays the circuit breaker's decision.
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
import ralph_review_deadlock  # noqa: E402
import ralph_review_respond  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def story(number=57):
    return {"number": number, "title": "Deadlock escalates to a human",
            "body": "## Acceptance Criteria\n\n- [ ] two rounds escalate\n",
            "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]}


def pull_request(head=HEAD, rounds=2):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": "OPEN", "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": [{"body": ralph_review.review_marker(head)}
                        for _ in range(rounds)],
            "comments": []}


def finding(ident="F-1", blocking=True, category="missing_tests"):
    return {"id": ident, "blocking": blocking, "category": category,
            "claim": "The size guard is never exercised.",
            "evidence": "No fixture in test/unit exceeds the byte limit.",
            "requirement": "Acceptance criterion: oversized output is covered.",
            "verification": "Add a fixture over the limit and assert refusal."}


def review(round_no, findings, verdict="request_changes"):
    return {"body": ralph_review.result_record(
        {"contract": "ralph-review/v1", "verdict": verdict, "head": HEAD,
         "model": "gpt-5-codex", "round": round_no,
         "summary": "Still unconvinced.", "findings": findings})}


def answer(round_no, ident="F-1",
           disposition=ralph_review_respond.DISPUTED):
    disposition_entry = {"id": ident, "disposition": disposition,
                         "note": "The behaviour is already covered."}
    if disposition == ralph_review_respond.DISPUTED:
        disposition_entry["evidence"] = "test_review_result.py:210 loads a " \
                                        "payload over the limit."
    return {"body": ralph_review.response_record(
        {"contract": ralph_review_respond.CONTRACT_VERSION, "head": HEAD,
         "round": round_no, "model": "claude-opus-5",
         "summary": "Disputed with evidence.",
         "dispositions": [disposition_entry]})}


def deadlocked():
    """Two rounds, the same blocker upheld, the same dispute standing."""
    return [review(1, [finding()]), answer(1),
            review(2, [finding()]), answer(2)]


class WhatIsStillOpen(unittest.TestCase):
    def test_an_upheld_blocker_carries_every_answer_it_was_given(self):
        open_findings = ralph_review_deadlock.unsettled(deadlocked())

        self.assertEqual([d.finding["id"] for d in open_findings], ["F-1"])
        dispute = open_findings[0]
        self.assertEqual([a["disposition"] for a in dispute.answers],
                         [ralph_review_respond.DISPUTED] * 2)

    def test_each_answer_says_which_round_it_came_from(self):
        # An argument that developed across rounds reads differently from one
        # made once, and the human arbitrating is reading for exactly that.
        dispute = ralph_review_deadlock.unsettled(deadlocked())[0]

        self.assertEqual([a["round"] for a in dispute.answers], [1, 2])

    def test_a_non_blocking_remark_is_not_a_deadlock(self):
        comments = [review(1, [finding(), finding("F-2", blocking=False,
                                                  category="style_preference")]),
                    answer(1)]

        open_findings = ralph_review_deadlock.unsettled(comments)

        self.assertEqual([d.finding["id"] for d in open_findings], ["F-1"])

    def test_a_finding_the_last_round_withdrew_is_settled(self):
        comments = [review(1, [finding()]), answer(1),
                    review(2, [], verdict="approve")]

        self.assertEqual(ralph_review_deadlock.unsettled(comments), [])


class TheEscalationComment(unittest.TestCase):
    def body(self, comments=None):
        return ralph_review_deadlock.escalation_comment(
            story(), ralph_review_deadlock.unsettled(comments or deadlocked()),
            rounds=2, handle="someone")

    def test_it_states_both_sides_of_every_unsettled_finding(self):
        body = self.body()

        self.assertIn("F-1", body)
        # The reviewer's case...
        self.assertIn("The size guard is never exercised.", body)
        self.assertIn("No fixture in test/unit exceeds the byte limit.", body)
        # ...and the implementation's.
        self.assertIn("The behaviour is already covered.", body)
        self.assertIn("test_review_result.py:210", body)

    def test_it_reports_the_rounds_this_story_itself_spent(self):
        """The deadlock report is about the disagreement it names."""
        body = ralph_review_deadlock.escalation_comment(
            story(), ralph_review_deadlock.unsettled(deadlocked()),
            rounds=ralph_review.rounds_spent(deadlocked()), handle="someone")

        self.assertEqual(ralph_review.rounds_spent(deadlocked()), 2)
        self.assertIn("2", body)

    def test_it_names_the_human_and_says_the_models_are_finished(self):
        body = self.body()

        self.assertIn("@someone", body)
        self.assertIn("2", body)
        self.assertIn("#57", body)


class TheEscalationPlan(unittest.TestCase):
    def plan(self, pr=None, comments=None, handle="someone"):
        return ralph_review_deadlock.escalate_plan(
            story(), pr or pull_request(),
            ralph_review_deadlock.unsettled(comments or deadlocked()),
            rounds=2, handle=handle)

    def test_it_asks_the_human_on_the_pull_request_then_blocks_the_story(self):
        plan = self.plan()

        self.assertTrue(plan.ok, plan.errors)
        summary, request, label = plan.commands
        self.assertEqual(summary[:3], ["gh", "pr", "comment"])
        self.assertIn("70", summary)
        self.assertEqual(request[:3], ["gh", "pr", "edit"])
        self.assertIn("--add-reviewer", request)
        self.assertIn("someone", request)
        self.assertEqual(label[:3], ["gh", "issue", "edit"])
        self.assertIn("state:blocked", label)
        self.assertIn("state:in-review", label)

    def test_the_human_reads_the_argument_before_being_asked_to_settle_it(self):
        plan = self.plan()

        self.assertLess(plan.commands.index(next(
            c for c in plan.commands if c[1] == "pr" and c[2] == "comment")),
            plan.commands.index(next(
                c for c in plan.commands if c[1] == "pr" and c[2] == "edit")))

    def test_it_never_labels_needs_human_or_halts_the_loop(self):
        # Deadlock blocks one Story. The global halt is the circuit breaker's
        # decision, made from how many Stories are blocked -- not this one's.
        flat = " ".join(" ".join(c) for c in self.plan().commands)

        self.assertNotIn("needs-human", flat)

    def test_an_unmarked_pull_request_is_outside_automated_review(self):
        plan = self.plan(pr=dict(pull_request(), body="a human pull request"))

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_a_negotiation_with_nothing_open_is_not_a_deadlock(self):
        plan = self.plan(comments=[review(1, [], verdict="approve")])

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class CliEscalateReview(unittest.TestCase):
    """Executed against a fake `gh` that logs its argv."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log = os.path.join(self.root, "calls.log")
        self._write(".ralph.yml", "version: 1\ngating:\n  - name: test\n"
                                  "    run: 'true'\nreview:\n  max_rounds: 2\n"
                                  "notify:\n  github: someone\n")
        self._write("story.json", json.dumps(story()))
        self._write("pr.json", json.dumps(pull_request()))
        self._write("issue.json", json.dumps({"comments": deadlocked()}))
        path = self._write("gh", "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\n'
                     'if [[ "$1 $2" == "issue view" ]]; then\n'
                     '  cat "%s/issue.json"\n'
                     'fi\n'
                     'exit 0\n' % self.root)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def run_cli(self):
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log)
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--escalate-review",
             os.path.join(self.root, "story.json"), ".ralph.yml", self.root,
             "--pr", os.path.join(self.root, "pr.json")],
            cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_the_story_is_blocked_and_the_configured_human_is_asked(self):
        proc, calls = self.run_cli()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gh pr comment 70", calls)
        self.assertIn("gh pr edit 70 --add-reviewer someone", calls)
        self.assertIn("gh issue edit 57 --add-label state:blocked "
                      "--remove-label state:in-review", calls)

    def test_both_arguments_reach_the_pull_request(self):
        _proc, calls = self.run_cli()

        self.assertIn("The size guard is never exercised.", calls)
        self.assertIn("test_review_result.py:210", calls)

    def test_the_loop_is_not_halted_and_no_other_story_is_touched(self):
        _proc, calls = self.run_cli()

        self.assertNotIn("needs-human", calls)
        # Exactly one issue was edited: this Story's.
        self.assertEqual(calls.count("gh issue edit"), 1)


if __name__ == "__main__":
    unittest.main()
