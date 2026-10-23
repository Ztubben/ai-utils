"""Contract tests for the exact-head, diff-first Review Agent bundle (#50)."""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_handoff  # noqa: E402
import ralph_review  # noqa: E402
import ralph_review_context  # noqa: E402
import ralph_review_respond  # noqa: E402
import ralph_review_result  # noqa: E402
import ralph_review_round  # noqa: E402


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
        "reviewThreads": [
            {"author": {"login": "carl"}, "path": "lib/a.py", "line": 3,
             "body": "F-1 is right, but fix it in the caller instead."},
        ],
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

    def test_a_human_reply_in_a_review_thread_reaches_the_next_round(self):
        # A human who steps into the threads before escalation is arbitrating
        # early. That is evidence the next fresh reviewer must see -- it lives
        # in the inline threads, not in the pull request's own comments.
        result = self.build()

        self.assertIn("fix it in the caller instead", result.text)
        self.assertIn("carl", result.text)

    def test_bundle_excludes_story_session_material_and_handoffs(self):
        result = self.build()
        self.assertNotIn(ralph_handoff.HANDOFF_MARKER, result.text)
        self.assertNotIn("private session state", result.text)
        self.assertNotIn("Depends on: #49", result.text)
        self.assertIn("may be explored read-only", result.text)

    def test_only_the_reviewer_is_told_the_checkout_is_read_only(self):
        # The Implementation Agent answering a round must append commits;
        # handed the reviewer's boundary, it obeyed it instead (PR #94).
        answer = self.build(for_role="implementation")
        self.assertTrue(answer.ok)
        self.assertNotIn("read-only", answer.text)
        self.assertNotIn("create commits", answer.text)
        self.assertIn("create commits", self.build(for_role="review").text)

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


class TolerantDecoding(unittest.TestCase):
    """#110: one undecodable byte in a reviewed diff wedged a Story in review,
    every Tick, until a human intervened. The review path decodes git and
    gh output and repository evidence as UTF-8 with replacement instead."""

    # GBK, as a CAD tool wrote it into a STEP file; a form feed, which
    # str.splitlines() would also have treated as a line break.
    GBK = b"PRODUCT ( '\xc8\xce\xba\xce' );\x0c tail\n"

    def git(self, root, *args):
        return subprocess.check_output(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=root, stderr=subprocess.STDOUT).decode("utf-8", "replace").strip()

    def test_a_diff_with_invalid_utf8_builds_and_still_anchors_findings(self):
        with tempfile.TemporaryDirectory() as root:
            self.git(root, "init", "-q")
            with open(os.path.join(root, "a.txt"), "w") as fh:
                fh.write("one\n")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "base")
            base = self.git(root, "rev-parse", "HEAD")
            with open(os.path.join(root, "part.step"), "wb") as fh:
                fh.write(b"HEADER;\n" + self.GBK + b"ENDSEC;\n")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "vendor file")
            head = self.git(root, "rev-parse", "HEAD")

            diff, changed = ralph_review_context.head_diff(
                {"baseRefOid": base, "headRefOid": head}, root)
            self.assertIn("\ufffd", diff)
            self.assertEqual(changed, ["part.step"])
            self.assertEqual(ralph_review_result.changed_lines(diff),
                             {"part.step": {1, 2, 3}})

    def test_repository_evidence_with_invalid_utf8_is_read(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "AGENTS.md"), "wb") as fh:
                fh.write(b"rules " + self.GBK)
            guidance, _ = ralph_review_context.repository_evidence(root, [])
            self.assertIn("\ufffd", guidance[0][1])

    def test_gh_and_git_output_on_the_round_and_respond_paths_is_tolerant(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("gh", "git"):
                path = os.path.join(root, name)
                with open(path, "w") as fh:
                    fh.write("#!/usr/bin/env bash\nprintf 'x\\310\\316y'\n")
                os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
            old = os.environ["PATH"]
            os.environ["PATH"] = root + os.pathsep + old
            try:
                self.assertEqual(ralph_review_round._gh(["x"], root), "x\ufffd\ufffdy")
                self.assertEqual(ralph_review_respond._gh(["x"], root), "x\ufffd\ufffdy")
                self.assertEqual(ralph_review_respond.Checkout(root).head(), "x\ufffd\ufffdy")
            finally:
                os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main()
