"""Contract tests for the protected control plane (#60).

A target repository declares the paths that govern Ralph's own review gate --
review workflows, prompts, schemas, configuration, override policy. A change
touching them always requires a native human approval before it can merge or be
parked for the bench, whatever the Story's type. The mechanism must not be able
to approve changes to itself.
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
import ralph_review_complete  # noqa: E402
import ralph_review_wait  # noqa: E402

HEAD = "9f1c2d3e4b5a60718293a4b5c6d7e8f90a1b2c3d"
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")


def story(number=60, type_="afk"):
    body = ("## Acceptance Criteria\n\n- [ ] protected paths need a human\n\n"
            "Parent: None\nDepends on: None\n")
    if type_ == "hil":
        body += "\n## Bench Test Procedure\n- poke it\n"
    return {"number": number, "title": "Control plane", "body": body,
            "state": "OPEN",
            "labels": [{"name": "state:in-review"}, {"name": "type:" + type_},
                       {"name": "prio:1"}]}


def pull_request(head=HEAD):
    return {"number": 70, "headRefOid": head, "baseRefOid": "b" * 40,
            "state": "OPEN", "body": ralph_review.MANAGED_PR_MARKER,
            "reviews": [], "comments": [],
            "statusCheckRollup": [
                {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"context": "ralph/model-review", "state": "SUCCESS"}]}


def human_approval(head=HEAD):
    return [{"body": ralph_review.arbitration_record(
        {"review": "R-1", "decision": "APPROVED", "reviewer": "carl",
         "head": head, "overrode": []})}]


class DeclaringTheControlPlane(unittest.TestCase):
    """AC: the config accepts protected patterns, validated by --check-config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config(self, extra=""):
        path = os.path.join(self.tmp.name, "ralph.yml")
        with open(path, "w") as fh:
            fh.write("version: 1\ngating:\n  - name: t\n    run: 'true'\n"
                     "notify:\n  github: someone\n" + extra)
        return path

    def test_a_declared_control_plane_validates(self):
        result = ralph_config.load_and_validate(self.config(
            "control_plane:\n  protected:\n    - prompts/**\n"
            "    - schema/*.json\n"))

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.config["control_plane"]["protected"],
                         ["prompts/**", "schema/*.json"])

    def test_a_repository_that_declares_nothing_protects_nothing(self):
        result = ralph_config.load_and_validate(self.config())

        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.config["control_plane"]["protected"], [])

    def test_a_pattern_escaping_the_repository_is_refused(self):
        result = ralph_config.load_and_validate(self.config(
            "control_plane:\n  protected:\n    - ../elsewhere/**\n"))

        self.assertFalse(result.ok)
        self.assertIn("control_plane/protected/0", " ".join(result.errors))

    def test_an_absolute_pattern_is_refused(self):
        result = ralph_config.load_and_validate(self.config(
            "control_plane:\n  protected:\n    - /etc/**\n"))

        self.assertFalse(result.ok)
        self.assertIn("control_plane/protected/0", " ".join(result.errors))

    def test_an_unknown_control_plane_key_is_refused(self):
        result = ralph_config.load_and_validate(self.config(
            "control_plane:\n  categories:\n    - review\n"))

        self.assertFalse(result.ok)

    def test_the_cli_reports_it(self):
        proc = subprocess.run(
            [RALPH, "--check-config", self.config(
                "control_plane:\n  protected:\n    - prompts/**\n")],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertIn("prompts/**", proc.stdout.decode())


class MatchingAProtectedPath(unittest.TestCase):
    def matched(self, changed, patterns):
        return ralph_review_complete.protected_paths(changed, patterns)

    def test_a_glob_matches_anywhere_beneath_it(self):
        self.assertEqual(
            self.matched(["prompts/review.v1.md", "lib/thing.py"],
                         ["prompts/**"]),
            ["prompts/review.v1.md"])

    def test_a_bare_directory_protects_everything_under_it(self):
        self.assertEqual(
            self.matched(["schema/review.schema.json"], ["schema"]),
            ["schema/review.schema.json"])

    def test_an_exact_file_is_its_own_pattern(self):
        self.assertEqual(self.matched([".ralph.yml"], [".ralph.yml"]),
                         [".ralph.yml"])

    def test_an_unprotected_change_matches_nothing(self):
        self.assertEqual(self.matched(["lib/thing.py"], ["prompts/**"]), [])

    def test_no_declared_patterns_protect_nothing(self):
        self.assertEqual(self.matched(["prompts/review.v1.md"], []), [])


class TheGateOverAProtectedPath(unittest.TestCase):
    def gate(self, comments=None, protected=("prompts/review.v1.md",)):
        return ralph_review_complete.gate_for(
            pull_request(), comments or [], protected=protected)

    def test_an_approving_model_review_alone_does_not_open_it(self):
        # AC: the mechanism can never approve changes to itself.
        gate = self.gate()

        self.assertFalse(gate.ok)
        self.assertTrue(gate.held_for_human)
        self.assertIn("prompts/review.v1.md", " ".join(gate.errors))

    def test_a_human_approval_opens_it(self):
        gate = self.gate(comments=human_approval())

        self.assertTrue(gate.ok, gate.errors)
        self.assertFalse(gate.held_for_human)

    def test_an_unprotected_change_needs_no_human(self):
        gate = self.gate(protected=())

        self.assertTrue(gate.ok, gate.errors)

    def test_a_hold_needs_everything_else_to_be_ready_first(self):
        # Telling the human "over to you" before CI has even finished would ask
        # them to approve something nobody has checked yet.
        red = dict(pull_request(), statusCheckRollup=[
            {"name": "test", "status": "IN_PROGRESS", "conclusion": None},
            {"context": "ralph/model-review", "state": "SUCCESS"}])

        gate = ralph_review_complete.gate_for(
            red, [], protected=("prompts/review.v1.md",))

        self.assertFalse(gate.ok)
        self.assertFalse(gate.held_for_human)


class CompletionOverAProtectedPath(unittest.TestCase):
    def plan(self, type_="afk", comments=None,
             protected=("prompts/review.v1.md",)):
        return ralph_review_complete.completion_plan(
            story(type_=type_), pull_request(), comments or [],
            protected=protected)

    def test_an_afk_story_is_not_merged_on_a_model_review_alone(self):
        plan = self.plan()

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertIn("human approval", " ".join(plan.errors))

    def test_a_hil_story_is_not_parked_on_a_model_review_alone(self):
        plan = self.plan(type_="hil")

        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_a_human_approval_lets_either_type_through(self):
        for type_ in ("afk", "hil"):
            plan = self.plan(type_=type_, comments=human_approval())
            self.assertTrue(plan.ok, (type_, plan.errors))


class TellingTheHumanWhy(unittest.TestCase):
    """AC: the pull request states why human approval is required."""

    def plan(self, protected=("prompts/review.v1.md", "schema/review.schema.json")):
        return ralph_review_complete.hold_plan(
            story(), pull_request(), protected, handle="someone")

    def test_it_names_the_protected_paths_and_asks_the_configured_human(self):
        plan = self.plan()

        self.assertTrue(plan.ok, plan.errors)
        flat = [" ".join(c) for c in plan.commands]
        notice = next(c for c in flat if c.startswith("gh pr comment"))
        self.assertIn("prompts/review.v1.md", notice)
        self.assertIn("schema/review.schema.json", notice)
        self.assertIn("control plane", notice.lower())
        self.assertTrue(any("--add-reviewer someone" in c for c in flat), flat)

    def test_it_records_the_hold_so_the_notice_is_posted_once_per_head(self):
        flat = [" ".join(c) for c in self.plan().commands]

        record = next(c for c in flat if "ralph-control-plane-hold:v1" in c)
        self.assertIn(HEAD, record)
        self.assertTrue(ralph_review.control_plane_held(
            [{"body": record.split("--body ", 1)[1]}], HEAD))

    def test_a_hold_recorded_for_another_head_does_not_count(self):
        flat = [" ".join(c) for c in self.plan().commands]
        record = next(c for c in flat if "ralph-control-plane-hold:v1" in c)

        self.assertFalse(ralph_review.control_plane_held(
            [{"body": record.split("--body ", 1)[1]}], "0" * 40))

    def test_it_never_blocks_or_closes_the_story(self):
        flat = " ".join(" ".join(c) for c in self.plan().commands)

        self.assertNotIn("state:blocked", flat)
        self.assertNotIn("issue close", flat)


class TheReviewWindowOverAProtectedPath(unittest.TestCase):
    """The negotiation is over, but the Story is not: it waits on a person."""

    def step(self, comments=None, protected=("prompts/review.v1.md",)):
        return ralph_review_wait.next_step(
            pull_request(), comments or [], max_rounds=2, protected=protected)

    def test_a_ready_protected_change_asks_the_human_instead_of_completing(self):
        self.assertEqual(self.step(), ralph_review_wait.HOLD)

    def test_the_notice_is_asked_for_once_and_then_simply_waited_on(self):
        held = [{"body": ralph_review.control_plane_hold_record(
            HEAD, ["prompts/review.v1.md"])}]

        self.assertEqual(self.step(comments=held), ralph_review_wait.WAIT)

    def test_the_human_approval_completes_it(self):
        self.assertEqual(self.step(comments=human_approval()),
                         ralph_review_wait.COMPLETE)

    def test_an_unprotected_change_completes_without_a_human(self):
        self.assertEqual(self.step(protected=()),
                         ralph_review_wait.COMPLETE)


if __name__ == "__main__":
    unittest.main()
