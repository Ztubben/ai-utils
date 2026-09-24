"""The green gate means the whole suite ran (#86, PRD #85).

`test/run.sh` used to skip the bats orchestration tier when bats was absent, so
a local "green" -- and the Loop's own gating when it works on ai-utils -- could
mean half the suite ran. These tests pin the refusal and the tier report.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNNER = os.path.join(REPO_ROOT, "test", "run.sh")
README = os.path.join(REPO_ROOT, "README.md")


class GateRefusesAPartialSuite(unittest.TestCase):
    def test_missing_bats_fails_loudly_before_any_tier_runs(self):
        # A PATH holding only what the runner needs to reach its prerequisite
        # check -- deliberately no bats. python3 is included so that, were the
        # check missing, the unit tier would start and the test would notice.
        with tempfile.TemporaryDirectory() as bindir:
            for tool in ("bash", "dirname", "python3"):
                os.symlink(shutil.which(tool), os.path.join(bindir, tool))
            proc = subprocess.run(
                [shutil.which("bash"), RUNNER],
                env={"PATH": bindir},
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("bats is not installed", proc.stderr)
        self.assertNotIn("== unit tests ==", proc.stdout)

    def test_runner_names_every_tier_and_reports_what_ran(self):
        with open(RUNNER) as fh:
            script = fh.read()
        for tier in ("unit", "bats", "scenarios"):
            self.assertIn("ran+=(%s)" % tier, script)
        self.assertIn("tiers run:", script)
        self.assertNotIn("skipping", script)


class ReadmeNamesBatsAsRequired(unittest.TestCase):
    def test_requirements_section_names_bats(self):
        with open(README) as fh:
            text = fh.read()
        section = text.split("## Requirements", 1)[1].split("\n## ", 1)[0]
        self.assertIn("bats", section)


if __name__ == "__main__":
    unittest.main()
