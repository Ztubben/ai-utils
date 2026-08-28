"""Contract tests for the exact-head, diff-first Review Agent bundle (#50)."""
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_handoff  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402


HEAD = "b" * 40
BASE = "a" * 40


def story():
    return {
        "number": 50,
        "title": "Diff-first review context",
        "body": "## Acceptance Criteria\n- [ ] exact evidence\n\n"
                "Parent: #42\nDepends on: #49",
    }


def pull_request():
    return {
        "number": 70,
        "body": ralph_review.MANAGED_PR_MARKER,
        "baseRefOid": BASE,
        "headRefOid": HEAD,
        "statusCheckRollup": [{"name": "test", "conclusion": "SUCCESS"}],
        "comments": [
            {"author": {"login": "human"}, "body": "Please verify the caller."},
            {"author": {"login": "ralph"},
             "body": ralph_handoff.HANDOFF_MARKER + " private session state"},
        ],
        "reviews": [{"author": {"login": "reviewer"},
                     "state": "CHANGES_REQUESTED", "body": "Finding F-1"}],
    }


def negotiation():
    """One round on the record: findings raised, then the answer to them."""
    result = {"contract": "ralph-review/v1", "verdict": "request_changes",
              "head": HEAD, "model": "gpt-5-codex", "round": 1,
              "summary": "One blocker.",
              "findings": [{"id": "F-1", "blocking": True,
                            "category": "missing_tests",
                            "claim": "The size guard is untested.",
                            "evidence": "no fixture over the limit",
                            "requirement": "acceptance criterion 3",
                            "verification": "add an oversized fixture"}]}
    answer = {"contract": "ralph-response/v1", "head": HEAD, "round": 1,
              "model": "claude-opus-5", "summary": "Disputed with evidence.",
              "dispositions": [{"id": "F-1", "disposition": "disputed",
                                "note": "The fixture already exists.",
                                "evidence": "test_review_result.py:210"}]}
    return [{"body": ralph_review.result_record(result)},
            {"body": ralph_review.response_record(answer)}]


class ReviewContextBundle(unittest.TestCase):
    def build(self, pr=None, round_no=2, comments=None, for_role="review"):
        return ralph_review_context.build_context(
            story(), pr or pull_request(),
            "diff --git a/lib/a.py b/lib/a.py\n+fixed",
            [("AGENTS.md", "Follow repository rules.")],
            [("CONTEXT.md", "## Language\n\n**Finding**: evidence")],
            [{"name": "test", "conclusion": "SUCCESS"}], round_no,
            history=ralph_review.negotiation_history(comments or []),
            for_role=for_role)

    def test_bundle_contains_every_required_evidence_class(self):
        result = self.build()
        self.assertTrue(result.ok, result.errors)
        for expected in ("Exact head commit: " + HEAD, "Review round: 2",
                         "exact evidence", "diff --git", "AGENTS.md",
                         "CONTEXT.md", '"conclusion": "SUCCESS"',
                         "Please verify the caller.", "Finding F-1"):
            self.assertIn(expected, result.text)

    def test_bundle_excludes_story_session_material_and_handoffs(self):
        result = self.build()
        self.assertNotIn(ralph_handoff.HANDOFF_MARKER, result.text)
        self.assertNotIn("private session state", result.text)
        self.assertNotIn("Depends on: #49", result.text)
        self.assertIn("may be explored read-only", result.text)

    def test_exact_head_base_round_and_managed_marker_are_required(self):
        for field in ("headRefOid", "baseRefOid"):
            pr = pull_request()
            del pr[field]
            self.assertFalse(self.build(pr=pr).ok, field)
        pr = pull_request()
        pr["body"] = "human PR"
        self.assertFalse(self.build(pr=pr).ok)
        self.assertFalse(self.build(round_no=0).ok)

    def test_a_later_round_carries_the_open_findings_and_their_answers(self):
        result = self.build(round_no=2, comments=negotiation())

        self.assertTrue(result.ok, result.errors)
        self.assertIn("F-1", result.text)
        self.assertIn("The size guard is untested.", result.text)
        self.assertIn("disputed", result.text)
        self.assertIn("test_review_result.py:210", result.text)

    def test_a_later_round_is_told_to_adjudicate_rather_than_start_over(self):
        result = self.build(round_no=2, comments=negotiation())

        # Line wrapping is the renderer's business, not the directive's.
        low = " ".join(result.text.lower().split())
        self.assertIn("adjudicate", low)
        self.assertIn("regression", low)
        self.assertIn("does not extend the round limit", low)

    def test_the_answering_model_reads_the_history_but_not_the_reviewers_orders(self):
        # The same evidence, addressed to the other role: the Implementation
        # Agent needs what was raised and answered, and must not be handed
        # instructions written for whoever judges it.
        result = self.build(round_no=2, comments=negotiation(),
                            for_role="implementation")

        self.assertIn("F-1", result.text)
        self.assertIn("disputed", result.text)
        self.assertNotIn("adjudicate", result.text.lower())

    def test_round_one_is_a_full_review_with_nothing_narrowing_it(self):
        result = self.build(round_no=1)

        self.assertNotIn("adjudicate", result.text.lower())
        self.assertNotIn("Earlier Rounds", result.text)

    def test_repository_evidence_is_scoped_not_the_whole_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "lib"))
            os.makedirs(os.path.join(root, "unrelated"))
            os.makedirs(os.path.join(root, "docs", "adr"))
            files = {
                "AGENTS.md": "root rules",
                "lib/AGENTS.md": "lib rules",
                "lib/code.py": "changed",
                "unrelated/secret.txt": "must not enter prompt",
                "CONTEXT.md": "domain language",
                "docs/adr/0001.md": "decision",
            }
            for relative, content in files.items():
                with open(os.path.join(root, relative), "w") as fh:
                    fh.write(content)
            guidance, domain = ralph_review_context.repository_evidence(
                root, ["lib/code.py"])
            self.assertEqual([p for p, _ in guidance], ["AGENTS.md", "lib/AGENTS.md"])
            joined = repr((guidance, domain))
            self.assertIn("domain language", joined)
            self.assertIn("decision", joined)
            self.assertNotIn("must not enter prompt", joined)


if __name__ == "__main__":
    unittest.main()
