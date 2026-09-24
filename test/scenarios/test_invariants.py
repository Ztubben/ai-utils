"""Every loop invariant goes red on a world built to violate it (#88, PRD #85).

Without these, a green scenario could be green because an invariant never
looks at anything.  Each world starts as the happy path's untouched starting
world and is then edited by hand -- state file, call log or bare remote -- into
exactly one violation.
"""
import json
import os
import subprocess
import sys
import unittest

import contracts
import harness
import invariants

sys.path.insert(0, os.path.join(harness.REPO_ROOT, "lib"))
import ralph_review  # noqa: E402


class HandBuiltWorld(unittest.TestCase):
    def setUp(self):
        self.world = harness.World(harness.load("afk_happy_path"))
        self.addCleanup(self.world.cleanup)
        self.watch = invariants.Watch(self.world)
        self.head = self.git("rev-parse", "HEAD")
        self.watch.check()      # the starting world is the baseline, as in a run

    def git(self, *args):
        return subprocess.run(["git"] + list(args), cwd=self.world.checkout,
                              env=self.world.env, check=True,
                              stdout=subprocess.PIPE, text=True).stdout.strip()

    def edit_state(self, change):
        state = self.world.state()
        change(state)
        with open(self.world.state_path, "w") as fh:
            json.dump(state, fh)

    def comment(self, where, body):
        def add(state):
            where(state)["comments"].append({
                "id": 1, "node_id": "IC_1", "author": "x", "body": body,
                "createdAt": "2026-01-01T00:00:00Z", "url": "u"})
        self.edit_state(add)

    def log(self, **entry):
        base = {"tool": "codex", "role": "review", "phase": "review",
                "profile": "hand-built", "argv": [], "prompt": "", "story": 1,
                "head": self.head, "round": 1, "text": "ok",
                "usage": {"input_tokens": 10}, "rc": 0}
        base.update(entry)
        if "prompt" not in entry:
            # A prompt that honours its phase's contract, so each world stays
            # a violation of exactly the invariant it was built for.
            base["prompt"] = "\n".join(contracts.CONTRACTS[base["phase"]]["required"])
        with open(self.world.log_path, "a") as fh:
            fh.write(json.dumps(base) + "\n")

    def result(self, round_no=1):
        return ralph_review.result_record({
            "contract": "ralph-review/v1", "verdict": "approve", "head": self.head,
            "model": "m", "round": round_no, "summary": "s", "findings": []})

    def response(self, round_no=1):
        return ralph_review.response_record({
            "contract": "ralph-response/v1", "head": self.head, "round": round_no,
            "model": "m", "responses": []})

    def red(self, final=False):
        return sorted({v.invariant for v in self.watch.check(final=final)})

    # --- one world per invariant --------------------------------------------

    def test_untouched_starting_world_is_green_until_the_budget_ends(self):
        self.assertEqual(self.red(), [])
        self.assertEqual(self.red(final=True), ["terminal-within-budget"])

    def test_two_results_for_one_head_and_round(self):
        self.log()
        self.log()
        story = lambda s: s["issues"]["1"]
        self.comment(story, self.result())
        self.comment(story, self.result())
        self.assertEqual(self.red(), ["one-result-per-round"])

    def test_two_responses_for_one_head_and_round_even_on_the_pull_request(self):
        # The response of PR #94 was recorded on the pull request, not the
        # Story; the invariant counts it wherever it landed.
        self.log(phase="response", role="implementation")
        self.log(phase="response", role="implementation")

        def pr(state):
            state["pulls"].setdefault("9", {"comments": []})
            return state["pulls"]["9"]
        self.comment(pr, self.response())
        self.comment(pr, self.response())
        self.assertEqual(self.red(), ["one-response-per-round"])

    def test_more_invocations_than_the_config_allows(self):
        # afk_happy_path uses the defaults: max_attempts 3, max_rounds 2.
        for _ in range(3 + 2 * 2 + 1):
            self.log(phase="iteration", role="implementation", head=None, round=None)
        self.assertEqual(self.red(), ["invocation-limit"])

    def test_the_limit_is_read_from_the_config(self):
        config = os.path.join(self.world.checkout, ".ralph.yml")
        with open(config, "a") as fh:
            fh.write("limits:\n  max_attempts: 1\n")
        for _ in range(1 + 2 * 2 + 1):
            self.log(phase="iteration", role="implementation", head=None, round=None)
        violation, = self.watch.check()
        self.assertIn("limit 5", violation.detail)

    def test_a_rewritten_branch(self):
        self.git("checkout", "-qb", "ralph/1-x")
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        self.git("push", "-q", "origin", "HEAD:refs/heads/ralph/1-x")
        self.assertEqual(self.red(), [])
        self.git("commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
        self.git("push", "-qf", "origin", "HEAD:refs/heads/ralph/1-x")
        self.assertEqual(self.red(), ["branch-only-grows"])

    def test_a_deleted_branch_is_not_a_rewrite(self):
        self.git("push", "-q", "origin", "HEAD:refs/heads/ralph/1-x")
        self.red()
        self.git("push", "-q", "origin", "--delete", "ralph/1-x")
        self.assertEqual(self.red(), [])

    def test_a_result_backed_only_by_an_empty_run(self):
        self.log(text="", usage={"input_tokens": 0, "output_tokens": 0})
        self.comment(lambda s: s["issues"]["1"], self.result())
        self.assertEqual(self.red(), ["empty-runs-never-count"])

    def test_a_promotion_backed_only_by_an_empty_iteration(self):
        self.log(phase="iteration", role="implementation", head=None, round=None,
                 text="RALPH-STORY-COMPLETE", usage={"input_tokens": 0})
        with open(self.world.log_path, "a") as fh:
            fh.write(json.dumps({"tool": "gh", "rc": 0, "argv": [
                "issue", "edit", "1", "--add-label", "state:in-review"]}) + "\n")
        self.assertEqual(self.red(), ["empty-runs-never-count"])

    def test_a_responder_told_the_checkout_is_read_only(self):
        # PR #94: the reviewer's bundle line, reaching the responder.
        prompt = "\n".join(contracts.CONTRACTS["response"]["required"] + [
            contracts.BUNDLE_READ_ONLY + ". " + contracts.BUNDLE_NO_MUTATION + "."])
        self.log(phase="response", role="implementation", prompt=prompt)
        violations = self.watch.check()
        self.assertEqual({v.invariant for v in violations}, {"prompt-contract"})
        self.assertIn("forbidden 'The checkout may be explored read-only'",
                      " ".join(v.detail for v in violations))

    def test_a_reviewer_not_told_it_is_read_only(self):
        prompt = "\n".join(p for p in contracts.CONTRACTS["review"]["required"]
                           if p != contracts.REVIEW_READ_ONLY)
        self.log(phase="review", prompt=prompt)
        violation, = self.watch.check()
        self.assertIn("missing '%s'" % contracts.REVIEW_READ_ONLY, violation.detail)

    def test_a_story_still_open_when_the_budget_ends(self):
        self.edit_state(lambda s: s["issues"]["1"]["labels"].append("state:in-review"))
        self.assertEqual(self.red(final=True), ["terminal-within-budget"])
        self.edit_state(lambda s: s["issues"]["1"]["labels"].append("state:blocked"))
        self.assertEqual(self.red(final=True), [])


class ViolationReport(unittest.TestCase):
    def test_names_tick_invariant_records_and_trailing_calls(self):
        violation = invariants.Violation("one-result-per-round", "#1 head abc round 1 ...",
                                         [{"head": "abc", "round": 1}])
        text = invariants.report(4, [violation], "  gh issue view 1 -> 0")
        self.assertIn("tick 4: invariant one-result-per-round violated", text)
        self.assertIn('"head": "abc"', text)
        self.assertIn("last calls:\n  gh issue view 1 -> 0", text)

    def test_the_runner_stops_on_a_violation_mid_scenario(self):
        # A Story that can never finish is reported by the terminal invariant
        # on the last Tick, with the Tick named.
        # (An absent dependency counts as done, so it is an open one that
        # never closes -- a PRD, which is not itself a Story.)
        spec = harness.load("afk_happy_path")
        issues = spec["github"]["issues"]
        issues[0]["body"] = issues[0]["body"].replace("Depends on: None", "Depends on: #2")
        issues.append({"number": 2, "title": "Another Feature", "labels": ["prd"],
                       "body": "## Problem Statement\nx\n\nDepends on: None\n"})
        spec.update(ticks=2)
        with self.assertRaises(harness.ScenarioFailure) as caught:
            harness.run_scenario(spec)
        self.assertIn("tick 2: invariant terminal-within-budget violated", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
