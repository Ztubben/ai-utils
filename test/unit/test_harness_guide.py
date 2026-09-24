"""The scenario harness guide stays complete and linked (#97, PRD #85)."""
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GUIDE = os.path.join(REPO_ROOT, "docs", "scenario-harness.md")


def _read(*parts):
    with open(os.path.join(REPO_ROOT, *parts)) as fh:
        return fh.read()


class HarnessGuide(unittest.TestCase):
    def test_covers_every_part_a_contributor_touches(self):
        guide = _read("docs", "scenario-harness.md")
        for heading in ("## Writing a scenario", "## Agent profiles", "## Loop invariants",
                        "## Capturing a production incident", "## Committing red",
                        "## Worked example: autopilot_controller #64"):
            self.assertIn(heading, guide)
        self.assertIn("reproduce it as a scenario first", guide)

    def test_names_every_invariant_and_profile_the_harness_has(self):
        import sys
        sys.path.insert(0, os.path.join(REPO_ROOT, "test", "scenarios"))
        import invariants
        guide = _read("docs", "scenario-harness.md")
        for name, _ in invariants.INVARIANTS:
            self.assertIn("`%s`" % name, guide)
        agent = _read("test", "scenarios", "fakes", "agent")
        profiles = agent.split("PROFILES = {", 1)[1].split("\n}", 1)[0]
        for line in profiles.splitlines():
            line = line.strip()
            if line.startswith('"') and '": ' in line:
                self.assertIn("`%s`" % line.split('"')[1], guide)

    def test_readme_and_agents_link_to_it(self):
        self.assertIn("docs/scenario-harness.md", _read("README.md"))
        self.assertIn("docs/scenario-harness.md", _read("AGENTS.md"))


if __name__ == "__main__":
    unittest.main()
