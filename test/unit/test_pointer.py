"""The committed ai-utils pointer versus the ai-utils that runs (#96)."""
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_pointer  # noqa: E402

A, B = "a" * 40, "b" * 40


class Check(unittest.TestCase):
    def test_a_match_proceeds(self):
        self.assertEqual(ralph_pointer.check(A, A).kind, ralph_pointer.OK)

    def test_a_mismatch_is_refused_naming_both_commits(self):
        result = ralph_pointer.check(A, B, path="tools/ai-utils")
        self.assertFalse(result.ok)
        self.assertIn(A, result.message)
        self.assertIn(B, result.message)
        self.assertIn("tooling.allow_pointer_drift", result.message)

    def test_an_allowed_mismatch_proceeds_and_says_so(self):
        result = ralph_pointer.check(A, B, allow_drift=True)
        self.assertTrue(result.ok)
        self.assertEqual(result.kind, ralph_pointer.DRIFT_ALLOWED)
        self.assertIn(B, result.message)

    def test_nothing_mounted_means_nothing_to_compare(self):
        self.assertTrue(ralph_pointer.check(None, None).ok)


class Locate(unittest.TestCase):
    def test_ai_utils_as_the_checkout_root_is_not_mounted(self):
        # ADR-0001 amendment: ai-utils is its own target repository here.
        self.assertIsNone(ralph_pointer.locate(REPO_ROOT, REPO_ROOT))

    def test_a_nested_checkout_is_found_relative_to_the_superproject(self):
        with tempfile.TemporaryDirectory() as root:
            subprocess.run(["git", "init", "-q", root], check=True)
            nested = os.path.join(root, "tools", "ai-utils")
            os.makedirs(nested)
            found = ralph_pointer.locate(nested, root)
            self.assertEqual(found, (os.path.realpath(root), os.path.join("tools", "ai-utils")))


if __name__ == "__main__":
    unittest.main()
