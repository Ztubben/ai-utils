"""Redacted incident capture (#91, PRD #85).

A capture of a private repository is committed to a public one, so redaction
is a whitelist: what the Loop parses survives, and prose -- any line or record
string nobody listed -- does not.
"""
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_capture  # noqa: E402
import ralph_ledger  # noqa: E402
import ralph_review  # noqa: E402

HEAD = "d114bacc4a3f0d924d7ee4be305d5df3841d6b56"
SECRET = "the heading hold loop overwrites Autopilot::engage"


def result():
    return {"contract": "ralph-review/v1", "verdict": "request_changes", "head": HEAD,
            "model": "claude-opus-5", "round": 1, "summary": SECRET,
            "findings": [{"id": "F-1", "blocking": True, "category": "defect",
                          "claim": SECRET, "evidence": SECRET, "requirement": SECRET,
                          "verification": SECRET,
                          "location": {"path": "src/steering/firmware.rs", "line": 133}}]}


def captured():
    story_body = ("## What to build\n%s\n\n## Acceptance Criteria\n- [ ] %s\n\n"
                  "Parent: #52\nDepends on: #57\n" % (SECRET, SECRET))
    return {"repo": {"owner": "o", "name": "r"},
            "issues": {
                "64": {"number": 64, "title": "US: firmware autopilot task", "body": story_body,
                       "labels": ["type:afk"], "state": "OPEN", "url": "u", "comments": [
                           {"id": 1, "body": ralph_review.result_record(result())},
                           {"id": 2, "body": "Some operator note: " + SECRET}]},
                "52": {"number": 52, "title": "PRD: heading hold", "labels": ["prd"],
                       "body": "## Problem Statement\n" + SECRET, "state": "OPEN",
                       "url": "u", "comments": []}},
            "pulls": {"94": {
                "number": 94, "title": SECRET, "state": "OPEN",
                "body": ralph_review.MANAGED_PR_MARKER + "\n\nRefs #64\n\n" + SECRET,
                "headRefName": "ralph/64-us-firmware-autopilot-task",
                "baseRefName": "feature/52-prd-heading-hold",
                "comments": [{"id": 3, "body": "Ralph implementation response — round 1\n\n"
                                               "Implementing model: `gpt-5`\n"
                                               "Reviewed commit: %s\n\n%s\n\n"
                                               "**F-1** — unresolved\n\n%s" % (HEAD, SECRET, SECRET)}],
                "reviews": [{"id": 4, "body": ralph_review.review_marker(HEAD) + "\n" + SECRET}],
                "reviewComments": [{"id": 5, "path": "src/steering/firmware.rs",
                                    "body": "**F-1** (defect, blocking)\n\n" + SECRET}]}},
            "git": {"commits": [{"oid": HEAD, "subject": SECRET}]}}


class Redaction(unittest.TestCase):
    def setUp(self):
        self.state = ralph_capture.redact(captured())
        self.text = json.dumps(self.state)

    def test_no_prose_survives_anywhere(self):
        self.assertNotIn("heading hold", self.text)
        self.assertNotIn("Autopilot", self.text)
        self.assertNotIn("firmware", self.text)

    def test_the_records_the_loop_reads_survive_with_their_structure(self):
        story = self.state["issues"]["64"]
        record = ralph_review.latest_result(story["comments"], HEAD)
        self.assertEqual((record["verdict"], record["round"], record["head"]),
                         ("request_changes", 1, HEAD))
        finding = record["findings"][0]
        self.assertEqual((finding["id"], finding["blocking"], finding["category"]),
                         ("F-1", True, "defect"))
        self.assertEqual(finding["claim"], ralph_capture.REDACTED)
        review = self.state["pulls"]["94"]["reviews"][0]["body"]
        self.assertIn(ralph_review.review_marker(HEAD), review)

    def test_story_shape_and_generated_lines_survive(self):
        body = self.state["issues"]["64"]["body"]
        for line in ("## What to build", "## Acceptance Criteria", "- [ ] [redacted]",
                     "Parent: #52", "Depends on: #57"):
            self.assertIn(line, body)
        pr = self.state["pulls"]["94"]
        self.assertIn("Refs #64", pr["body"])
        self.assertIn(ralph_review.MANAGED_PR_MARKER, pr["body"])
        answer = pr["comments"][0]["body"]
        self.assertIn("**F-1** — unresolved", answer)
        self.assertIn("Implementing model: `gpt-5`", answer)
        self.assertTrue(pr["reviewComments"][0]["body"].startswith("**F-1** (defect, blocking)"))

    def test_paths_are_mapped_consistently_between_records_and_threads(self):
        record = ralph_review.latest_result(self.state["issues"]["64"]["comments"], HEAD)
        thread = self.state["pulls"]["94"]["reviewComments"][0]
        self.assertEqual(record["findings"][0]["location"]["path"], thread["path"])
        self.assertTrue(thread["path"].startswith("redacted/"))

    def test_titles_and_branches_are_renamed_the_way_the_loop_derives_them(self):
        self.assertEqual(self.state["issues"]["64"]["title"], "Story 64")
        self.assertEqual(self.state["issues"]["52"]["title"], "PRD 52")
        pr = self.state["pulls"]["94"]
        self.assertEqual(pr["headRefName"], "ralph/64-story-64")
        self.assertEqual(pr["baseRefName"], "feature/52-prd-52")
        self.assertEqual(self.state["git"]["commits"][0], {"oid": HEAD, "subject": "commit 1"})

    def test_a_ledger_payload_still_parses(self):
        event = {"version": "ralph-usage/v1", "run": "tick-1", "time": "2026-09-23T08:10:47Z",
                 "phase": "response", "role": "implementation", "provider": "codex",
                 "model": "gpt-5.6-sol", "story": 64, "pull_request": 94, "round": 1,
                 "head": HEAD, "usage": {"input": 10}, "availability": {"input": "reported"}}
        body = ralph_ledger.ledger_body(64, [event])
        state = captured()
        state["issues"]["64"]["comments"] = [{"id": 9, "body": body}]
        redacted = ralph_capture.redact(state)
        _, events = ralph_ledger.find_ledger(redacted["issues"]["64"]["comments"])
        self.assertEqual(events, [event])


if __name__ == "__main__":
    unittest.main()
