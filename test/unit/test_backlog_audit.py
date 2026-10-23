"""Tests for the ralph-backlog-audit skill's snapshot script.

The script must judge dependencies exactly as the selection engine does and
flag the drift the skill audits: lint failures, unreachable and not-planned
dependencies, cycles, finished-but-open stories, and completed PRDs.
"""
import importlib.util
import os
import unittest
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO_ROOT, "skills", "ralph-backlog-audit", "scripts",
                      "backlog_snapshot.py")
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)

spec = importlib.util.spec_from_file_location("backlog_snapshot", SCRIPT)
backlog_snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backlog_snapshot)


def _issue(number, labels, parent="None", deps="None", state="OPEN",
           reason="", updated="2026-10-20T00:00:00Z", body=None):
    if body is None:
        body = ("## Acceptance Criteria\n\n- [ ] works\n\n"
                "Parent: %s\nDepends on: %s\n" % (parent, deps))
    return {"number": number, "title": "issue %d" % number,
            "labels": [{"name": l} for l in labels], "body": body,
            "state": state, "stateReason": reason, "updatedAt": updated}


STORY = ["state:ready", "type:afk"]


def _flags(snap, number):
    entry = next(r for r in snap["open"] if r["number"] == number)
    return {f["check"]: f["detail"] for f in entry["flags"]}


class BacklogSnapshot(unittest.TestCase):

    def snap(self, issues, prs=(), commits=None, stale_days=45):
        return backlog_snapshot.snapshot(list(issues), list(prs), commits or {},
                                         stale_days, NOW)

    def test_canonical_story_has_no_flags(self):
        snap = self.snap([_issue(1, STORY)])
        self.assertEqual(_flags(snap, 1), {})
        self.assertEqual(snap["next_action"], "start #1")

    def test_lint_errors_come_from_the_story_linter(self):
        snap = self.snap([_issue(1, [], body="no shape at all")])
        self.assertTrue(any("state:*" in e for e in _flags(snap, 1)["lint"]))

    def test_dependency_on_another_features_story_is_unreachable(self):
        snap = self.snap([
            _issue(10, ["prd"], body="Depends on: None\n"),
            _issue(11, STORY, parent="#10", state="CLOSED", reason="COMPLETED"),
            _issue(12, STORY, deps="#11")])
        self.assertIn("#11: unreachable: closed story of another Feature",
                      _flags(snap, 12)["dependency-suspect"])

    def test_not_planned_dependency_is_suspect_though_satisfied(self):
        snap = self.snap([
            _issue(1, STORY, state="CLOSED", reason="NOT_PLANNED"),
            _issue(2, STORY, deps="#1")])
        self.assertIn("NOT_PLANNED", _flags(snap, 2)["dependency-suspect"][0])
        self.assertEqual(snap["next_action"], "start #2")

    def test_cycles_are_reported(self):
        snap = self.snap([_issue(1, STORY, deps="#2"), _issue(2, STORY, deps="#1")])
        self.assertEqual(len(snap["cycles"]), 1)
        self.assertEqual(set(snap["cycles"][0]), {1, 2})

    def test_commit_naming_an_open_story_is_implementation_evidence(self):
        snap = self.snap([_issue(1, STORY)], commits={1: ["abc123 feat: thing (#1)"]})
        self.assertEqual(_flags(snap, 1)["maybe-implemented"], ["abc123 feat: thing (#1)"])

    def test_in_review_story_with_merged_pr_is_state_drift(self):
        pr = {"number": 5, "title": "x", "body": "Closes #1", "state": "MERGED",
              "headRefName": "s", "mergedAt": "2026-10-01T00:00:00Z"}
        snap = self.snap([_issue(1, ["state:in-review", "type:afk"])], prs=[pr])
        self.assertIn("state-drift", _flags(snap, 1))

    def test_done_prd_without_state_ready_is_stuck(self):
        snap = self.snap([
            _issue(10, ["prd"], body="Depends on: None\n"),
            _issue(11, STORY, parent="#10", state="CLOSED", reason="COMPLETED")])
        self.assertIn("prd-stuck", _flags(snap, 10))

    def test_done_prd_with_state_ready_is_left_to_the_loop(self):
        snap = self.snap([
            _issue(10, ["prd", "state:ready"], body="Depends on: None\n"),
            _issue(11, STORY, parent="#10", state="CLOSED", reason="COMPLETED")])
        self.assertEqual(_flags(snap, 10), {})

    def test_closed_issue_still_carrying_a_state_label_is_listed(self):
        snap = self.snap([_issue(1, STORY, state="CLOSED", reason="COMPLETED")])
        self.assertEqual(snap["closed_with_state"], [{"number": 1, "state": "ready"}])

    def test_lowercase_acceptance_heading_is_called_out(self):
        body = "## Acceptance criteria\n\n- [ ] works\n\nParent: None\nDepends on: None\n"
        snap = self.snap([_issue(1, STORY, body=body)])
        self.assertTrue(any("does not recognize" in d for d in _flags(snap, 1)["lint"]))

    def test_untouched_issue_is_stale(self):
        snap = self.snap([_issue(1, STORY, updated="2026-01-01T00:00:00Z")])
        self.assertIn("stale", _flags(snap, 1))


if __name__ == "__main__":
    unittest.main()
