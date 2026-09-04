"""Contract tests for human arbitration (#58).

The human's controls are GitHub's own: Approve and Request changes. Approve is
authoritative and releases the model-review gate even over unresolved model
findings, because a model never holds authority over a human decision. Request
changes is authoritative feedback and goes back to the implementation model.
An ordinary comment is just a comment.
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
import ralph_review_human  # noqa: E402
import ralph_review_wait  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"


def story(number=58, state="blocked"):
    return {"number": number, "title": "Human arbitration",
            "body": "## Acceptance Criteria\n\n- [ ] approve releases the gate\n",
            "labels": [{"name": "state:" + state}, {"name": "type:afk"}]}


def ralph_review_body(head=HEAD):
    return ralph_review.review_marker(head) + "\nRalph model review — round 1"


def human(state, ident="R-1", author="carl", body="Looks right to me.",
          submitted="2026-08-28T10:00:00Z"):
    return {"id": ident, "state": state, "body": body,
            "author": {"login": author}, "submittedAt": submitted}


def comment(body, ident="IC-1", author="carl", created="2026-08-28T11:00:00Z"):
    return {"id": ident, "body": body, "author": {"login": author},
            "createdAt": created}


def pull_request(reviews=None, head=HEAD, comments=None):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": "OPEN", "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": reviews if reviews is not None else [
                {"state": "COMMENTED", "body": ralph_review_body(),
                 "author": {"login": "ralph"}, "id": "R-0"}],
            "comments": comments or []}


def open_findings():
    return [{"id": "F-1", "blocking": True, "category": "missing_tests",
             "claim": "The guard is untested.", "evidence": "no fixture",
             "requirement": "criterion 2", "verification": "add one"}]


def recorded_round(head=HEAD):
    return [{"body": ralph_review.result_record(
        {"contract": "ralph-review/v1", "verdict": "request_changes",
         "head": head, "model": "gpt-5-codex", "round": 2,
         "summary": "Still unconvinced.", "findings": open_findings()})}]


class ReadingTheHumansDecision(unittest.TestCase):
    def test_no_human_review_is_no_decision(self):
        self.assertIsNone(ralph_review_human.human_decision(pull_request()))

    def test_an_approval_is_a_decision(self):
        decision = ralph_review_human.human_decision(
            pull_request(reviews=[human("APPROVED")]))

        self.assertEqual(decision.state, ralph_review_human.APPROVED)
        self.assertEqual(decision.author, "carl")
        self.assertEqual(decision.id, "R-1")

    def test_requested_changes_is_a_decision_carrying_the_feedback(self):
        decision = ralph_review_human.human_decision(pull_request(reviews=[
            human("CHANGES_REQUESTED", body="Do it in the caller instead.")]))

        self.assertEqual(decision.state, ralph_review_human.CHANGES_REQUESTED)
        self.assertIn("caller", decision.body)

    def test_an_ordinary_comment_is_not_a_decision(self):
        # AC (#58): an ordinary human comment changes no label, check or state.
        self.assertIsNone(ralph_review_human.human_decision(pull_request(
            reviews=[human("COMMENTED", body="Nice one.")])))

    def test_ralphs_own_review_is_never_mistaken_for_a_human_one(self):
        self.assertIsNone(ralph_review_human.human_decision(pull_request(
            reviews=[{"state": "APPROVED", "body": ralph_review_body(),
                      "author": {"login": "ralph"}, "id": "R-9"}])))

    def test_the_latest_decision_is_the_one_that_counts(self):
        decision = ralph_review_human.human_decision(pull_request(reviews=[
            human("CHANGES_REQUESTED", ident="R-1"),
            human("APPROVED", ident="R-2")]))

        self.assertEqual(decision.id, "R-2")
        self.assertEqual(decision.state, ralph_review_human.APPROVED)

    def test_a_decision_already_acted_on_is_not_acted_on_twice(self):
        decision = ralph_review_human.human_decision(
            pull_request(reviews=[human("APPROVED")]))
        record = ralph_review.arbitration_record(
            {"review": decision.id, "decision": decision.state,
             "reviewer": decision.author, "head": HEAD, "overrode": []})

        self.assertTrue(ralph_review.arbitrated([{"body": record}], "R-1"))
        self.assertFalse(ralph_review.arbitrated([{"body": record}], "R-2"))


APPROVE = ralph_review_human.APPROVE_MARKER
REQUEST_CHANGES = ralph_review_human.REQUEST_CHANGES_MARKER
APPROVERS = ("carl",)


class TheApprovalMarker(unittest.TestCase):
    """The stand-in for a control GitHub will not give the author.

    GitHub offers the author of a pull request neither Approve nor Request
    changes, and Ralph opens every pull request as the operator's own account.
    Where Ralph has no identity of its own the control-plane hold would
    otherwise wait forever on a click that person is not allowed to make.
    """

    def decide(self, pr, approvers=APPROVERS):
        return ralph_review_human.human_decision(pr, approvers)

    def test_a_marker_comment_from_the_configured_approver_approves(self):
        decision = self.decide(pull_request(comments=[comment(APPROVE)]))

        self.assertEqual(decision.state, ralph_review_human.APPROVED)
        self.assertEqual(decision.author, "carl")
        self.assertEqual(decision.id, "IC-1")

    def test_the_request_changes_marker_sends_it_back(self):
        decision = self.decide(pull_request(comments=[
            comment("Use the caller's units.\n\n" + REQUEST_CHANGES)]))

        self.assertEqual(decision.state, ralph_review_human.CHANGES_REQUESTED)
        self.assertIn("caller", decision.body)

    def test_a_comment_review_carries_the_marker_too(self):
        # "Review changes -> Comment" is the other half of what GitHub leaves
        # open to the author, so it has to work the same way.
        decision = self.decide(pull_request(
            reviews=[human("COMMENTED", body=APPROVE)]))

        self.assertEqual(decision.state, ralph_review_human.APPROVED)

    def test_a_marker_from_anyone_else_carries_no_authority(self):
        # Anyone who can see a pull request can comment on it, and what this
        # releases is the gate guarding Ralph's own review machinery.
        self.assertIsNone(self.decide(pull_request(comments=[
            comment(APPROVE, author="a-passer-by")])))

    def test_without_a_configured_approver_only_github_decides(self):
        # A deployment where Ralph has its own identity needs no marker, and
        # must not have one: there the operator can simply click Approve.
        self.assertIsNone(self.decide(pull_request(comments=[comment(APPROVE)]),
                                      approvers=()))

    def test_the_marker_must_stand_alone_on_its_line(self):
        self.assertIsNone(self.decide(pull_request(comments=[
            comment("I would say `%s` but I want a second opinion." % APPROVE)])))

    def test_ralphs_own_notice_is_not_the_approval_it_teaches(self):
        # The hold notice has to quote the marker in order to explain it. If
        # that quotation read as a decision, Ralph would approve its own
        # control-plane changes the moment it asked a human not to.
        notice = ralph_review_complete.hold_notice(
            story(), [".ralph.yml"], handle="carl")

        self.assertIn(APPROVE, notice)
        self.assertIsNone(self.decide(pull_request(comments=[comment(notice)])))

    def test_the_marker_is_not_left_in_the_words_the_model_is_given(self):
        # The marker is addressed to Ralph, not to the model launched with this
        # feedback.
        decision = self.decide(pull_request(comments=[
            comment("%s\n\nRename it before you land it." % REQUEST_CHANGES)]))

        self.assertNotIn(APPROVE, decision.body)
        self.assertNotIn(REQUEST_CHANGES, decision.body)
        self.assertEqual(decision.body, "Rename it before you land it.")

    def test_a_native_review_still_needs_no_marker_and_no_approver(self):
        decision = ralph_review_human.human_decision(
            pull_request(reviews=[human("APPROVED")]))

        self.assertEqual(decision.state, ralph_review_human.APPROVED)

    def test_the_latest_decision_wins_across_both_kinds(self):
        decision = self.decide(pull_request(
            reviews=[human("CHANGES_REQUESTED", ident="R-1",
                           submitted="2026-08-28T10:00:00Z")],
            comments=[comment(APPROVE, created="2026-08-28T12:00:00Z")]))

        self.assertEqual(decision.id, "IC-1")
        self.assertEqual(decision.state, ralph_review_human.APPROVED)

    def test_approvers_are_read_from_the_notify_handle(self):
        # No new configuration: that handle is already the person Ralph asks
        # for a review when it holds a change.
        self.assertEqual(
            ralph_review_human.approvers_from({"notify": {"github": "carl"}}),
            ("carl",))
        self.assertEqual(ralph_review_human.approvers_from({}), ())

    def test_the_hold_becomes_an_arbitration_the_poll_can_act_on(self):
        # The whole point: a held Story moves again on the marker.
        step = ralph_review_wait.next_step(
            pull_request(comments=[comment(APPROVE)]), [],
            max_rounds=2, protected=(".ralph.yml",), approvers=APPROVERS)

        self.assertEqual(step, ralph_review_wait.ARBITRATE)

    def test_without_the_marker_that_same_poll_still_holds(self):
        step = ralph_review_wait.next_step(
            pull_request(comments=[comment("Looks fine to me.")]), [],
            max_rounds=2, protected=(".ralph.yml",), approvers=APPROVERS)

        self.assertNotEqual(step, ralph_review_wait.ARBITRATE)


class Approval(unittest.TestCase):
    """Approve is authoritative: it releases the gate over open findings."""

    def plan(self, comments=None, decision=None):
        return ralph_review_human.approval_plan(
            story(), pull_request(reviews=[human("APPROVED")]),
            decision or ralph_review_human.human_decision(
                pull_request(reviews=[human("APPROVED")])),
            comments if comments is not None else recorded_round())

    def commands(self, **kwargs):
        return [" ".join(c) for c in self.plan(**kwargs).commands]

    def test_the_required_check_is_released_on_the_reviewed_head(self):
        flat = self.commands()

        status = next(c for c in flat if "statuses/" + HEAD in c)
        self.assertIn("state=success", status)
        self.assertIn("context=" + ralph_review_human.CHECK_CONTEXT, status)
        self.assertIn("carl", status)

    def test_the_escalation_is_cleared_and_the_story_returns_to_review(self):
        flat = self.commands()

        label = next(c for c in flat if c.startswith("gh issue edit"))
        self.assertIn("--add-label state:in-review", label)
        self.assertIn("--remove-label state:blocked", label)

    def test_the_override_is_recorded_on_the_story_with_what_it_overrode(self):
        flat = self.commands()

        record = next(c for c in flat if "ralph-human-arbitration:v1" in c)
        self.assertIn("carl", record)
        self.assertIn("F-1", record)
        self.assertIn("APPROVED", record)

    def test_an_approval_with_nothing_open_still_records_the_decision(self):
        flat = self.commands(comments=[])

        self.assertTrue(any("ralph-human-arbitration:v1" in c for c in flat))


class RequestedChanges(unittest.TestCase):
    """Authoritative feedback: back to the implementation model, with it."""

    def decision(self, body="Do it in the caller instead."):
        return ralph_review_human.human_decision(pull_request(
            reviews=[human("CHANGES_REQUESTED", body=body)]))

    def test_the_story_returns_to_in_review(self):
        plan = ralph_review_human.reopen_plan(story(), self.decision())

        self.assertTrue(plan.ok, plan.errors)
        label = " ".join(plan.commands[0])
        self.assertIn("--add-label state:in-review", label)
        self.assertIn("--remove-label state:blocked", label)

    def test_the_prompt_hands_the_model_the_humans_own_words(self):
        prompt = ralph_review_human.arbitration_prompt(
            self.decision(), "# Ralph Review Context v1\n")

        self.assertIn("Do it in the caller instead.", prompt)
        self.assertIn("carl", prompt)
        self.assertIn("# Ralph Review Context v1", prompt)


class ArbitrationPromptV1(unittest.TestCase):
    """The judgement half is checked in, so it is drift-guarded."""

    def setUp(self):
        self.assertTrue(os.path.isfile(ralph_review_human.ARBITRATION_PROMPT),
                        "prompts/arbitration.v1.md must be checked in")
        with open(ralph_review_human.ARBITRATION_PROMPT) as fh:
            self.text = fh.read()

    def test_the_humans_decision_outranks_any_model_finding(self):
        low = " ".join(self.text.lower().split())
        self.assertIn("authoritative", low)
        self.assertIn("do not dispute", low)

    def test_it_keeps_the_append_only_and_gating_discipline(self):
        low = " ".join(self.text.lower().split())
        self.assertIn("never amend", low)
        self.assertIn("force-push", low)
        self.assertIn("ralph --run-gating", low)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
