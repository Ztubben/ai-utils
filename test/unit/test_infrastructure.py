"""Contract tests for infrastructure failures and reviewer reassignment (#61).

A provider outage, a quota or authentication refusal, and output the wrapper
cannot publish are all infrastructure problems rather than substantive
disagreement, so none of them may spend a Negotiation Round.  Ralph retries them
inside the waiting policy and, when the window closes, checkpoints and resumes
with the round count untouched.  Replacing a Story's assigned reviewer is the
one thing Ralph never does on its own: it is a human action that rewrites the
durable assignment and leaves an audit record.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_agent  # noqa: E402
import ralph_config  # noqa: E402
import ralph_models  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_respond  # noqa: E402
import ralph_review_round  # noqa: E402
import ralph_review_wait  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def pull_request(head=HEAD, reviews=None, state="OPEN"):
    return {"number": 61, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": state, "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": reviews or [], "comments": []}


class Clock:
    def __init__(self):
        self.t = 0
        self.sleeps = []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class InfrastructureFailureInTheWindow(unittest.TestCase):
    """A failure the provider caused is retried; the round count is untouched."""

    def setUp(self):
        self.clock = Clock()
        self.state = {"pr": pull_request(), "comments": []}
        self.acts = []

    def await_(self, act, window=100, first_poll=30):
        return ralph_review_wait.await_review(
            ralph_review_wait.WaitPolicy(window_seconds=window,
                                         first_poll=first_poll),
            fetch=lambda: self.state["pr"], act=act,
            read_comments=lambda: self.state["comments"],
            sleep=self.clock.sleep, now=self.clock.now)

    def failing(self, exit_code):
        def act(step, pull_request_):
            self.acts.append(step)
            return ralph_review_wait.step_outcome(exit_code, "review round")
        return act

    def test_a_provider_outage_is_retried_until_the_window_closes(self):
        # The reviewer never finished, so there is no judgement to act on and
        # nothing to leave for the next tick *yet*: the window is still open,
        # and the outage may well be over by the next poll.
        result = self.await_(self.failing(ralph_agent.EXIT_INFRASTRUCTURE_FAILURE))

        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)
        self.assertGreater(len(self.acts), 1)
        self.assertEqual(set(self.acts), {ralph_review_wait.REVIEW})

    def test_retrying_an_outage_consumes_no_negotiation_round(self):
        result = self.await_(self.failing(ralph_agent.EXIT_INFRASTRUCTURE_FAILURE))

        self.assertEqual(result.retries, len(self.acts))
        self.assertEqual(ralph_review.review_stamps(self.state["pr"]), [])
        self.assertEqual(ralph_review_round.next_round(self.state["pr"]), 1)

    def test_output_the_wrapper_would_not_publish_is_retried_too(self):
        # The reviewer answered with something unpublishable, so nothing was
        # posted and the head is still unjudged. That is the provider's failure,
        # not the negotiation's, so it costs no round either.
        result = self.await_(self.failing(ralph_review_round.EXIT_INVALID_OUTPUT))

        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)
        self.assertGreater(result.retries, 1)
        self.assertEqual(ralph_review_round.next_round(self.state["pr"]), 1)

    def test_an_unpublishable_answer_is_the_same_kind_of_failure(self):
        # The wrapper rejects a malformed answer from the Implementation Agent
        # for the same reason it rejects a malformed review: nothing reached the
        # pull request, so the round the answer was owed is still owed.
        self.assertEqual(
            ralph_review_respond.EXIT_CODES[ralph_review_respond.INVALID_OUTPUT],
            ralph_review_round.EXIT_INVALID_OUTPUT)
        self.assertIn(ralph_review_round.EXIT_INVALID_OUTPUT,
                      ralph_review_wait.RETRYABLE_EXITS)

    def test_rewritten_history_is_a_refusal_not_an_outage(self):
        # An amend or a force-push is the model breaking the protocol, not the
        # provider failing: relaunching it every poll would spend an invocation
        # each time on a branch it has already stranded.
        self.assertNotIn(
            ralph_review_respond.EXIT_CODES[ralph_review_respond.NOT_APPEND_ONLY],
            ralph_review_wait.RETRYABLE_EXITS)

    def test_a_refusal_is_not_retried_it_would_refuse_identically(self):
        # An unmarked pull request, a bad config, a role resolution that was
        # refused: the same call fails the same way on the next poll.
        result = self.await_(self.failing(2))

        self.assertEqual(result.kind, ralph_review_wait.FAILED)
        self.assertEqual(len(self.acts), 1)

    def test_a_session_limit_ends_the_window_rather_than_relaunching(self):
        # The budget the retry would spend is the very thing that ran out.
        result = self.await_(self.failing(ralph_agent.EXIT_SESSION_EXHAUSTED))

        self.assertEqual(result.kind, ralph_review_wait.FAILED)
        self.assertEqual(len(self.acts), 1)

    def test_a_window_spent_on_outages_ends_owing_a_handoff(self):
        # EXPIRED is the one ending that obliges the tick to checkpoint, and it
        # is not a failure: the Story is intact and the next tick resumes it.
        result = self.await_(self.failing(ralph_agent.EXIT_INFRASTRUCTURE_FAILURE))

        self.assertEqual(result.kind, ralph_review_wait.EXPIRED)
        self.assertTrue(result.errors, "the outage is reported, not swallowed")

    def test_the_next_tick_resumes_the_round_the_outage_never_spent(self):
        self.await_(self.failing(ralph_agent.EXIT_INFRASTRUCTURE_FAILURE))

        # Whatever a later tick reads off GitHub, it reads what this one found:
        # an unjudged head owed round one.
        self.assertEqual(
            ralph_review_wait.next_step(self.state["pr"], self.state["comments"],
                                        max_rounds=2),
            ralph_review_wait.REVIEW)
        self.assertEqual(ralph_review_round.next_round(self.state["pr"]), 1)


def config_of(name):
    validated = ralph_config.load_and_validate(
        os.path.join(FIXTURES, "valid", name))
    assert validated.ok, validated.errors
    return validated.config


def story(labels=("model:impl:claude-opus-5", "model:review:gpt-5-codex")):
    return {"number": 61, "title": "Retry an outage",
            "body": "## Acceptance Criteria\n\n- [ ] retries\n",
            "labels": [{"name": "state:in-review"}, {"name": "type:afk"}]
                      + [{"name": name} for name in labels]}


class ReassignmentIsHumanOnly(unittest.TestCase):
    """Ralph never substitutes a model; a human does, and it leaves a record."""

    def test_an_assigned_story_is_never_reassigned_by_the_loop(self):
        # The one automated path that writes assignment labels heals forward
        # only: with both roles recorded it has nothing to say.
        plan = ralph_models.assign_plan(story(),
                                        config_of("models-reassigned.yml"))

        self.assertTrue(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertEqual(plan.newly_assigned, [])
        self.assertEqual(plan.review.model, "gpt-5-codex")

    def test_no_automated_path_can_reach_the_reassignment_command(self):
        # A drift guard, not a style rule: reassignment is human-only, so the
        # unattended tick must not be able to call it even by accident.
        with open(os.path.join(REPO_ROOT, "bin", "ralph.sh")) as fh:
            tick = fh.read()
        self.assertNotIn("--reassign-model", tick)

    def test_a_human_reassignment_replaces_the_durable_assignment(self):
        plan = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "claude-review", reason="codex has been down all week")

        self.assertTrue(plan.ok, plan.errors)
        edit = [c for c in plan.commands if c[:3] == ["gh", "issue", "edit"]]
        self.assertEqual(len(edit), 1)
        self.assertIn("--add-label", edit[0])
        self.assertEqual(edit[0][edit[0].index("--add-label") + 1],
                         "model:review:claude-sonnet-5")
        self.assertIn("--remove-label", edit[0])
        self.assertEqual(edit[0][edit[0].index("--remove-label") + 1],
                         "model:review:gpt-5-codex")

    def test_the_reassignment_records_an_audit_comment_last(self):
        # Last, so a crash leaves the labels right and the record missing --
        # a record of something that did not happen is the worse failure.
        plan = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "claude-review", reason="codex has been down all week")

        comment = plan.commands[-1]
        self.assertEqual(comment[:4], ["gh", "issue", "comment", "61"])
        body = comment[comment.index("--body") + 1]
        self.assertIn(ralph_models.REASSIGNMENT_MARKER, body)
        self.assertIn("gpt-5-codex", body)
        self.assertIn("claude-sonnet-5", body)
        self.assertIn("codex has been down all week", body)

    def test_the_implementation_role_is_left_exactly_as_it_was(self):
        plan = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "claude-review", reason="why")

        for command in plan.commands:
            self.assertNotIn("model:impl:claude-opus-5", command)

    def test_reassigning_to_the_model_already_recorded_is_refused(self):
        plan = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "codex-review", reason="why")

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_a_reassignment_that_collapses_the_pair_needs_acknowledgement(self):
        # Independence is the invariant the two roles exist to provide; losing
        # it by hand is allowed, losing it by accident is not.
        plan = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "claude-impl", reason="only one provider is up")

        self.assertFalse(plan.ok)
        self.assertIn("--allow-same-model", " ".join(plan.errors))

        accepted = ralph_models.reassign_plan(
            story(), config_of("models-reassigned.yml"), "review",
            "claude-impl", reason="only one provider is up",
            allow_same_model=True)
        self.assertTrue(accepted.ok, accepted.errors)

    def test_an_unassigned_role_is_not_a_reassignment(self):
        # There is nothing to replace: starting the Story records it (#46).
        plan = ralph_models.reassign_plan(
            story(labels=()), config_of("models-reassigned.yml"), "review",
            "claude-review", reason="why")

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class CliReassignModel(unittest.TestCase):
    """Executed against a mocked `gh`: labels move, and the record is written."""

    def _run(self, tmp, args, story_obj=None):
        log = os.path.join(tmp, "calls.log")
        path = os.path.join(tmp, "gh")
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "gh $*" >> "$RALPH_LOG"\nexit 0\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        proc = subprocess.run(
            [RALPH, "--reassign-model", "-"] + list(args),
            cwd=tmp, input=json.dumps(story_obj or story()),
            env=dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                     RALPH_LOG=log),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        calls = ""
        if os.path.exists(log):
            with open(log) as fh:
                calls = fh.read()
        return proc, calls

    def test_it_moves_the_label_and_writes_the_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, calls = self._run(tmp, [
                "review", "claude-review", valid("models-reassigned.yml"),
                "--reason", "codex quota exhausted for the day"])

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--add-label model:review:claude-sonnet-5", calls)
            self.assertIn("--remove-label model:review:gpt-5-codex", calls)
            self.assertIn("issue comment 61", calls)
            self.assertIn("codex quota exhausted for the day", calls)

    def test_a_reassignment_without_a_reason_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, calls = self._run(tmp, [
                "review", "claude-review", valid("models-reassigned.yml")])

            self.assertEqual(proc.returncode, 2)
            self.assertEqual(calls, "")
            self.assertIn("reason", proc.stderr)

    def test_an_unknown_profile_key_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc, calls = self._run(tmp, [
                "review", "no-such-profile", valid("models-reassigned.yml"),
                "--reason", "why"])

            self.assertEqual(proc.returncode, 2)
            self.assertEqual(calls, "")


def valid(name):
    return os.path.join(FIXTURES, "valid", name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
