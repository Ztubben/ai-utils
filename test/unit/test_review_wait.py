"""Contract tests for bounded in-tick review waiting (#54).

Waiting is the orchestration process's job, not a model's: the tick holds its
lock, polls durable GitHub state with backoff, launches an agent only when
there is work, and gives up on a bounded window with a Handoff. These tests
drive the clock and the fetches, so nothing here actually sleeps.
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

import ralph_config  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_wait  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def pull_request(head=HEAD, reviews=None, state="OPEN"):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": state, "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": reviews or [], "comments": []}


class NextStep(unittest.TestCase):
    """What the tick may do right now, read off durable GitHub state alone."""

    def test_an_unreviewed_head_is_work_to_do(self):
        self.assertEqual(ralph_review_wait.next_step(pull_request()),
                         ralph_review_wait.REVIEW)

    def test_a_head_that_carries_its_review_is_waited_on_not_reviewed_again(self):
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])
        self.assertEqual(ralph_review_wait.next_step(reviewed),
                         ralph_review_wait.WAIT)

    def test_there_is_nothing_to_wait_for_without_a_marked_pull_request(self):
        self.assertEqual(ralph_review_wait.next_step(None),
                         ralph_review_wait.GONE)
        human = dict(pull_request(), body="a human pull request")
        self.assertEqual(ralph_review_wait.next_step(human),
                         ralph_review_wait.GONE)

    def test_a_review_requesting_changes_is_answered_not_waited_on(self):
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])
        record = ralph_review.result_record(
            {"head": HEAD, "round": 1, "verdict": "request_changes",
             "findings": [{"id": "F-1", "blocking": True}]})

        self.assertEqual(
            ralph_review_wait.next_step(reviewed, comments=[{"body": record}]),
            ralph_review_wait.RESPOND)

    def test_an_approving_review_is_not_something_to_answer(self):
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])
        record = ralph_review.result_record(
            {"head": HEAD, "round": 1, "verdict": "approve", "findings": []})

        self.assertEqual(
            ralph_review_wait.next_step(reviewed, comments=[{"body": record}]),
            ralph_review_wait.WAIT)

    def changes_requested(self, round_no=1):
        return {"body": ralph_review.result_record(
            {"head": HEAD, "round": round_no, "verdict": "request_changes",
             "findings": [{"id": "F-1", "blocking": True}]})}

    def answer(self, round_no=1):
        return {"body": ralph_review.response_record(
            {"head": HEAD, "round": round_no, "dispositions": []})}

    def test_an_answered_head_is_judged_again_rather_than_answered_twice(self):
        # A dispute answers the round without moving the head, so the same
        # commit goes back to a fresh reviewer to withdraw or uphold.
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])

        self.assertEqual(
            ralph_review_wait.next_step(
                reviewed, comments=[self.changes_requested(), self.answer()]),
            ralph_review_wait.REVIEW)

    def test_the_upheld_finding_is_answered_again_not_re_reviewed(self):
        # Round two judged the same head and still requests changes: the ball
        # is back with the Implementation Agent, not the reviewer.
        twice = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)},
            {"body": ralph_review.review_marker(HEAD)}])

        self.assertEqual(
            ralph_review_wait.next_step(
                twice, comments=[self.changes_requested(), self.answer(),
                                 self.changes_requested(round_no=2)]),
            ralph_review_wait.RESPOND)

    def stamps(self, count):
        return pull_request(reviews=[{"body": ralph_review.review_marker(HEAD)}
                                     for _ in range(count)])

    def test_the_last_round_still_requesting_changes_goes_to_a_human(self):
        # Two rounds spent and the reviewer is still asking: the models have
        # had their say, and the disagreement is now the human's to settle.
        self.assertEqual(
            ralph_review_wait.next_step(
                self.stamps(2),
                comments=[self.changes_requested(), self.answer(),
                          self.changes_requested(round_no=2)],
                max_rounds=2),
            ralph_review_wait.ESCALATE)

    def test_a_dispute_in_the_last_round_goes_to_a_human_not_a_third_review(self):
        self.assertEqual(
            ralph_review_wait.next_step(
                self.stamps(2),
                comments=[self.changes_requested(), self.answer(),
                          self.changes_requested(round_no=2),
                          self.answer(round_no=2)],
                max_rounds=2),
            ralph_review_wait.ESCALATE)

    def test_the_budget_is_the_storys_own_not_its_pull_requests(self):
        """PRD #69: a Story spends only the rounds recorded against it.

        Reviews the Story did not itself spend can no longer exhaust its
        budget -- which is exactly how a Feature's third Story once escalated
        at the limit having never been reviewed once.
        """
        self.assertEqual(
            ralph_review_wait.next_step(
                self.stamps(3), comments=[self.changes_requested()],
                max_rounds=2),
            ralph_review_wait.RESPOND)

    def test_a_story_with_no_recorded_round_is_never_escalated(self):
        elsewhere = pull_request(reviews=[
            {"body": ralph_review.review_marker("0" * 40)} for _ in range(5)])

        self.assertEqual(
            ralph_review_wait.next_step(elsewhere, comments=[], max_rounds=1),
            ralph_review_wait.REVIEW)

    def test_a_negotiation_inside_its_budget_keeps_negotiating(self):
        self.assertEqual(
            ralph_review_wait.next_step(
                self.stamps(1), comments=[self.changes_requested()],
                max_rounds=2),
            ralph_review_wait.RESPOND)

    def test_an_agreed_review_never_escalates_however_many_rounds_it_took(self):
        approved = {"body": ralph_review.result_record(
            {"head": HEAD, "round": 2, "verdict": "approve", "findings": []})}

        self.assertEqual(
            ralph_review_wait.next_step(
                self.stamps(2),
                comments=[self.changes_requested(), self.answer(), approved],
                max_rounds=2),
            ralph_review_wait.WAIT)

    def approving(self, ident="R-1"):
        return {"id": ident, "state": "APPROVED", "body": "Ship it.",
                "author": {"login": "carl"}}

    def acted_on(self, ident="R-1", decision="APPROVED", head=HEAD):
        return {"body": ralph_review.arbitration_record(
            {"review": ident, "decision": decision, "reviewer": "carl",
             "head": head, "overrode": ["F-1"]})}

    def test_a_human_decision_outranks_anything_the_models_are_owed(self):
        # AC (#58): a human review is acted on before another round or answer.
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}, self.approving()])

        self.assertEqual(
            ralph_review_wait.next_step(
                reviewed, comments=[self.changes_requested()], max_rounds=2),
            ralph_review_wait.ARBITRATE)

    def test_an_approval_already_acted_on_completes_the_story(self):
        # AC (#58/#59): the gate is satisfied, so the Story is finished even
        # though a blocking model finding is still on the record.
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}, self.approving()])

        self.assertEqual(
            ralph_review_wait.next_step(
                reviewed,
                comments=[self.changes_requested(), self.acted_on()],
                max_rounds=2),
            ralph_review_wait.COMPLETE)

    def test_an_approval_of_an_older_head_does_not_carry_to_the_new_one(self):
        moved = pull_request(head="c" * 40, reviews=[self.approving()])

        self.assertEqual(
            ralph_review_wait.next_step(
                moved, comments=[self.acted_on()], max_rounds=2),
            ralph_review_wait.REVIEW)

    def test_a_satisfied_review_check_with_green_ci_completes_the_story(self):
        # AC (#59): an approving model review satisfies the gate on its own.
        approved = dict(pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}]),
            statusCheckRollup=[
                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"context": "ralph/model-review", "state": "SUCCESS"}])

        self.assertEqual(
            ralph_review_wait.next_step(approved, comments=[], max_rounds=2),
            ralph_review_wait.COMPLETE)

    def test_a_satisfied_review_but_pending_ci_keeps_waiting(self):
        # AC (#59): it merges only when *both* halves hold for this head.
        pending = dict(pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}]),
            statusCheckRollup=[
                {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
                {"context": "ralph/model-review", "state": "SUCCESS"}])

        self.assertEqual(
            ralph_review_wait.next_step(pending, comments=[], max_rounds=2),
            ralph_review_wait.WAIT)

    def test_an_ordinary_human_comment_changes_nothing(self):
        # AC (#58): a comment is a comment; the negotiation carries on.
        reviewed = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)},
            {"id": "R-1", "state": "COMMENTED", "body": "Nice.",
             "author": {"login": "carl"}}])

        self.assertEqual(
            ralph_review_wait.next_step(
                reviewed, comments=[self.changes_requested()], max_rounds=2),
            ralph_review_wait.RESPOND)

    def test_a_closed_pull_request_ends_the_negotiation(self):
        self.assertEqual(ralph_review_wait.next_step(pull_request(state="MERGED")),
                         ralph_review_wait.GONE)


class Backoff(unittest.TestCase):
    def test_polls_back_off_from_the_configured_interval(self):
        policy = ralph_review_wait.WaitPolicy(window_seconds=600, first_poll=30)
        self.assertEqual([policy.sleep_for(i, elapsed=0) for i in range(4)],
                         [30, 60, 120, 240])

    def test_backoff_is_capped_so_a_long_window_keeps_polling(self):
        policy = ralph_review_wait.WaitPolicy(window_seconds=3600, first_poll=30)
        self.assertEqual(policy.sleep_for(20, elapsed=0),
                         ralph_review_wait.MAX_POLL_SECONDS)

    def test_the_last_sleep_stops_at_the_window_rather_than_overrunning_it(self):
        policy = ralph_review_wait.WaitPolicy(window_seconds=100, first_poll=30)
        self.assertEqual(policy.sleep_for(3, elapsed=90), 10)

    def test_an_exhausted_window_asks_for_no_further_sleep(self):
        policy = ralph_review_wait.WaitPolicy(window_seconds=100, first_poll=30)
        self.assertEqual(policy.sleep_for(0, elapsed=100), 0)
        self.assertTrue(policy.expired(elapsed=100))
        self.assertFalse(policy.expired(elapsed=99))


class WindowFromConfig(unittest.TestCase):
    def policy(self, review=""):
        path = os.path.join(self.tmp.name, "ralph.yml")
        with open(path, "w") as fh:
            fh.write("version: 1\ngating:\n  - name: t\n    run: 'true'\n"
                     "notify:\n  github: someone\n" + review)
        validated = ralph_config.load_and_validate(path)
        self.assertTrue(validated.ok, validated.errors)
        return ralph_review_wait.WaitPolicy.from_config(validated.config)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_the_window_defaults_to_sixty_minutes(self):
        policy = self.policy()
        self.assertEqual(policy.window_seconds, 60 * 60)
        self.assertEqual(policy.first_poll, 30)

    def test_a_target_repository_can_shorten_the_window(self):
        policy = self.policy("review:\n  wait_minutes: 5\n  poll_seconds: 10\n")
        self.assertEqual(policy.window_seconds, 300)
        self.assertEqual(policy.first_poll, 10)

    def test_the_negotiation_gets_two_rounds_unless_configured_otherwise(self):
        self.assertEqual(self.policy().max_rounds, 2)
        self.assertEqual(self.policy("review:\n  max_rounds: 4\n").max_rounds, 4)

    def test_a_round_limit_below_one_is_not_a_valid_config(self):
        path = os.path.join(self.tmp.name, "ralph.yml")
        with open(path, "w") as fh:
            fh.write("version: 1\ngating:\n  - name: t\n    run: 'true'\n"
                     "notify:\n  github: someone\nreview:\n  max_rounds: 0\n")

        validated = ralph_config.load_and_validate(path)

        self.assertFalse(validated.ok)
        self.assertIn("max_rounds", " ".join(validated.errors))


class Clock:
    """A driven clock: nothing sleeps, but elapsed time still advances."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class AwaitReview(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = {"pr": pull_request(), "comments": []}
        self.acts = []

    def fetch(self):
        return self.state["pr"]

    def comments(self):
        return self.state["comments"]

    def act(self, step, pull_request_):
        """Stand in for a Negotiation Round: it publishes a review."""
        self.acts.append(step)
        self.state["pr"] = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])
        return True, [], False

    def await_(self, window=100, first_poll=30):
        return ralph_review_wait.await_review(
            ralph_review_wait.WaitPolicy(window_seconds=window,
                                         first_poll=first_poll),
            fetch=self.fetch, act=self.act, read_comments=self.comments,
            sleep=self.clock.sleep, now=self.clock.now)

    def test_it_reviews_the_head_once_then_waits_out_the_window(self):
        result = self.await_()

        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)
        self.assertEqual(len(self.acts), 1)
        self.assertEqual(result.invocations, 1)
        self.assertLessEqual(sum(self.clock.sleeps), 100)
        self.assertGreater(result.polls, 1)

    def test_waiting_on_an_already_reviewed_head_makes_no_invocation(self):
        self.state["pr"] = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])

        result = self.await_()

        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)
        self.assertEqual(self.acts, [])
        self.assertEqual(result.invocations, 0)

    def test_the_answer_to_a_review_leaves_a_head_that_is_reviewed_again(self):
        # The negotiation advances inside one window: review, answer, review.
        # The answer appends a commit, so the new head has no review yet.
        answered = "1" * 40

        def act(step, pull_request_):
            self.acts.append(step)
            head = pull_request_["headRefOid"]
            if step == ralph_review_wait.REVIEW:
                self.state["pr"] = pull_request(head=head, reviews=[
                    {"body": ralph_review.review_marker(head)}])
                self.state["comments"] += [{"body": ralph_review.result_record(
                    {"head": head, "round": 1, "verdict": "request_changes",
                     "findings": [{"id": "F-1", "blocking": True}]})}]
            else:
                self.state["pr"] = pull_request(head=answered)
            return True, [], False

        self.act = act
        result = self.await_(window=100, first_poll=30)

        self.assertEqual(self.acts[:3], [ralph_review_wait.REVIEW,
                                         ralph_review_wait.RESPOND,
                                         ralph_review_wait.REVIEW])
        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)

    def test_a_deadlock_ends_the_wait_and_spends_no_invocation(self):
        # There is nothing left to wait for: the Story is blocked and a human
        # has been asked. Sitting out the rest of the window would hold the
        # tick's lock over work that has already stopped.
        self.state["pr"] = pull_request(reviews=[
            {"body": ralph_review.review_marker(HEAD)}])
        self.state["comments"] = [{"body": ralph_review.result_record(
            {"head": HEAD, "round": 1, "verdict": "request_changes",
             "findings": [{"id": "F-1", "blocking": True}]})}]

        result = ralph_review_wait.await_review(
            ralph_review_wait.WaitPolicy(window_seconds=100, first_poll=30,
                                         max_rounds=1),
            fetch=self.fetch, act=self.act, read_comments=self.comments,
            sleep=self.clock.sleep, now=self.clock.now)

        self.assertEqual(result.kind, ralph_review_wait.ESCALATE)
        self.assertEqual(self.acts, [ralph_review_wait.ESCALATE])
        self.assertEqual(result.invocations, 0)

    def test_a_step_that_failed_is_left_for_the_next_tick_not_retried(self):
        # A step that failed on its own terms rather than the provider's -- gh
        # refused the review here -- gets one retry on the next tick, not one
        # per poll. (An outage is the other case, and #61 rides that one out
        # inside the window instead.)
        self.act = lambda step, pr: (False, ["gh refused the review"], False)

        result = self.await_()

        self.assertEqual(result.kind, ralph_review_wait.FAILED)
        self.assertEqual(result.invocations, 1)
        self.assertEqual(self.clock.sleeps, [])

    def test_a_merged_pull_request_ends_the_wait_immediately(self):
        self.state["pr"] = pull_request(state="MERGED")

        result = self.await_()

        self.assertEqual(result.kind, ralph_review_wait.GONE)
        self.assertEqual(result.polls, 1)
        self.assertEqual(self.clock.sleeps, [])


class CliAwaitReview(unittest.TestCase):
    """Executed against a mock `gh`, with a window short enough to close."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.log = os.path.join(self.root, "calls.log")
        self._write(".ralph.yml",
                    "version: 1\ngating:\n  - name: t\n    run: 'true'\n"
                    "review:\n  wait_minutes: 0.005\n  poll_seconds: 0.01\n"
                    "notify:\n  github: someone\n")
        self._write("story.json", json.dumps({
            "number": 54, "title": "Wait for review",
            "body": "## Acceptance Criteria\n\n- [ ] waits\n",
            "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]}))

    def _write(self, name, text):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path

    def _gh(self, prs, view):
        self._write("prs.json", json.dumps(prs))
        self._write("pr.json", json.dumps(view))
        path = self._write("gh", "")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\n'
                     'if [[ "$1 $2" == "pr list" ]]; then cat "%(r)s/prs.json"; fi\n'
                     'if [[ "$1 $2" == "pr view" ]]; then cat "%(r)s/pr.json"; fi\n'
                     'exit 0\n' % {"r": self.root})
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def _provider_that_must_not_run(self):
        for name in ("claude", "codex"):
            path = self._write(name, "")
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s LAUNCHED" >> "$RALPH_LOG"\nexit 1\n' % name)
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_await(self):
        env = dict(os.environ, PATH=self.root + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=self.log)
        for name in ("RALPH_CLAUDE", "RALPH_CODEX"):
            env.pop(name, None)
        proc = subprocess.run(
            [os.path.join(REPO_ROOT, "bin", "ralph"), "--await-review",
             os.path.join(self.root, "story.json"), ".ralph.yml", self.root],
            cwd=self.root, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(self.log):
            with open(self.log) as fh:
                calls = fh.read()
        return proc, calls

    def test_a_reviewed_head_is_waited_on_without_launching_any_model(self):
        head = "a" * 40
        marked = ralph_review.MANAGED_PR_MARKER + "\n\nRefs #54\n"
        self._gh([{"number": 70, "body": marked}],
                 {"number": 70, "body": marked, "state": "OPEN",
                  "headRefOid": head, "baseRefOid": "b" * 40,
                  "reviews": [{"body": ralph_review.review_marker(head)}],
                  "comments": []})
        self._provider_that_must_not_run()

        proc, calls = self.run_await()

        self.assertEqual(proc.returncode, ralph_review_wait.EXIT_WINDOW_EXPIRED,
                         proc.stderr)
        self.assertNotIn("LAUNCHED", calls)
        self.assertGreater(calls.count("gh pr view"), 1)  # it polled
        self.assertIn("window", proc.stdout + proc.stderr)

    def test_nothing_to_negotiate_ends_the_wait_at_once(self):
        self._gh([{"number": 12, "body": "a human pull request"}], {})
        self._provider_that_must_not_run()

        proc, calls = self.run_await()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("LAUNCHED", calls)


if __name__ == "__main__":
    unittest.main()
