"""Tests that the authoring skills (ralph-story, to-issues, to-prd) emit
the amended backlog encoding (ADR-0002, ADR-0006).

Every story carries a `Parent:` line; PRDs carry the `prd` label and their
own `Depends on:` line; story-level dependencies stay within a Feature;
and the breakdown flow applies `state:ready` to the PRD as its final act.
"""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS = os.path.join(REPO_ROOT, "skills")


def _read_skill(name):
    path = os.path.join(SKILLS, name, "SKILL.md")
    with open(path) as fh:
        return fh.read()


class RalphStorySkillEncoding(unittest.TestCase):
    """ralph-story SKILL.md must document the amended encoding."""

    def setUp(self):
        self.text = _read_skill("ralph-story")

    def test_template_includes_parent_line(self):
        self.assertIn("Parent:", self.text,
                      "the body template must include a `Parent:` line")

    def test_documents_parent_none_for_orphan_stories(self):
        self.assertIn("Parent: None", self.text,
                      "must document `Parent: None` for Orphan Stories")

    def test_documents_cross_feature_dependency_prohibition(self):
        self.assertIn("cross-Feature", self.text,
                      "must document the cross-Feature dependency prohibition")

    def test_uses_hil_not_hitl(self):
        # The body text may reference HITL only in the context of forbidding it.
        lines = [line for line in self.text.splitlines()
                 if "HITL" in line and "not HITL" not in line
                 and "never HITL" not in line and "use HIL" not in line.lower()]
        self.assertEqual(lines, [],
                         "must use HIL terminology, not HITL (except to forbid it)")


class ToIssuesSkillEncoding(unittest.TestCase):
    """to-issues SKILL.md must emit the amended encoding."""

    def setUp(self):
        self.text = _read_skill("to-issues")

    def test_template_includes_parent_line(self):
        self.assertIn("Parent:", self.text,
                      "the issue template must include a `Parent:` line")

    def test_documents_parent_none_for_orphan_stories(self):
        self.assertIn("Parent: None", self.text,
                      "must document `Parent: None` for Orphan Stories")

    def test_template_uses_depends_on_not_blocked_by(self):
        self.assertIn("Depends on:", self.text,
                      "must use `Depends on:` line, not `Blocked by:`")

    def test_documents_state_ready_on_prd_at_end_of_breakdown(self):
        self.assertIn("state:ready", self.text,
                      "the breakdown flow must apply `state:ready` to the PRD")

    def test_documents_cross_feature_dependency_prohibition(self):
        self.assertIn("cross-Feature", self.text,
                      "must document the cross-Feature dependency prohibition")

    def test_uses_hil_not_hitl(self):
        lines = [line for line in self.text.splitlines()
                 if "HITL" in line and "not HITL" not in line
                 and "never HITL" not in line and "use HIL" not in line.lower()]
        self.assertEqual(lines, [],
                         "must use HIL terminology, not HITL (except to forbid it)")


class ToPrdSkillEncoding(unittest.TestCase):
    """to-prd SKILL.md must emit the amended PRD encoding."""

    def setUp(self):
        self.text = _read_skill("to-prd")

    def test_applies_prd_label(self):
        self.assertIn("prd", self.text,
                      "must instruct applying the `prd` label")

    def test_template_includes_depends_on_line(self):
        self.assertIn("Depends on:", self.text,
                      "the PRD template must include a `Depends on:` line")

    def test_does_not_use_ready_for_agent_label(self):
        self.assertNotIn("ready-for-agent", self.text,
                         "must use the `prd` label, not `ready-for-agent`")

    def test_documents_cross_feature_dependency_prohibition(self):
        self.assertIn("cross-Feature", self.text,
                      "must document the cross-Feature dependency prohibition "
                      "or the PRD-level alternative")


if __name__ == "__main__":
    unittest.main()
