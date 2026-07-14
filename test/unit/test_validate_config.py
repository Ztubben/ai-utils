"""Unit tests for the .ralph.yml config validator (US-002).

Pure-logic tests over good/malformed config fixtures, plus a couple of
subprocess checks of `bin/ralph --check-config` exit codes.
"""
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures", "config")
SAMPLE = os.path.join(REPO_ROOT, ".ralph.yml.sample")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_config  # noqa: E402


def valid(name):
    return os.path.join(FIXTURES, "valid", name)


def invalid(name):
    return os.path.join(FIXTURES, "invalid", name)


class ValidConfigTests(unittest.TestCase):
    def test_minimal_config_is_valid(self):
        result = ralph_config.load_and_validate(valid("minimal.yml"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.errors, [])

    def test_full_config_is_valid(self):
        result = ralph_config.load_and_validate(valid("full.yml"))
        self.assertTrue(result.ok, result.errors)

    def test_shipped_sample_config_validates(self):
        self.assertTrue(os.path.exists(SAMPLE), "a documented sample .ralph.yml must ship")
        result = ralph_config.load_and_validate(SAMPLE)
        self.assertTrue(result.ok, result.errors)

    def test_defaults_are_applied_when_omitted(self):
        result = ralph_config.load_and_validate(valid("minimal.yml"))
        self.assertEqual(result.config["branching"]["base"], "develop")
        self.assertEqual(result.config["limits"]["max_attempts"], 3)
        self.assertEqual(result.config["limits"]["circuit_breaker"], 2)

    def test_feature_and_rescue_pattern_defaults_when_omitted(self):
        result = ralph_config.load_and_validate(valid("minimal.yml"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.config["branching"]["feature_pattern"],
                         "feature/{issue}-{slug}")
        self.assertEqual(result.config["branching"]["rescue_pattern"],
                         "rescue/{issue}-{slug}")

    def test_explicit_values_override_defaults(self):
        result = ralph_config.load_and_validate(valid("full.yml"))
        self.assertEqual(result.config["limits"]["max_attempts"], 5)
        self.assertEqual(result.config["limits"]["circuit_breaker"], 3)

    def test_summary_mentions_key_settings(self):
        result = ralph_config.load_and_validate(valid("full.yml"))
        summary = result.summary()
        self.assertIn("develop", summary)
        self.assertIn("build", summary)
        self.assertIn("test", summary)

    def test_summary_includes_feature_and_rescue_patterns(self):
        result = ralph_config.load_and_validate(valid("minimal.yml"))
        summary = result.summary()
        self.assertIn("feature/{issue}-{slug}", summary)
        self.assertIn("rescue/{issue}-{slug}", summary)


class InvalidConfigTests(unittest.TestCase):
    def _assert_invalid_mentioning(self, fixture, needle):
        result = ralph_config.load_and_validate(invalid(fixture))
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)
        joined = "\n".join(result.errors)
        self.assertIn(needle, joined,
                      "expected an error mentioning %r, got: %s" % (needle, joined))

    def test_missing_gating_is_rejected(self):
        self._assert_invalid_mentioning("missing-gating.yml", "gating")

    def test_bad_afk_merge_is_rejected(self):
        self._assert_invalid_mentioning("bad-afk-merge.yml", "afk_merge")

    def test_feature_pattern_missing_issue_placeholder_is_rejected(self):
        self._assert_invalid_mentioning("bad-feature-pattern.yml", "feature_pattern")

    def test_rescue_pattern_missing_issue_placeholder_is_rejected(self):
        self._assert_invalid_mentioning("bad-rescue-pattern.yml", "rescue_pattern")

    def test_wrong_types_are_rejected(self):
        self._assert_invalid_mentioning("wrong-types.yml", "max_attempts")

    def test_label_override_is_rejected(self):
        # The canonical state:/type:/prio: label scheme is mandated, not overridable.
        self._assert_invalid_mentioning("label-override.yml", "labels")

    def test_empty_config_is_rejected(self):
        result = ralph_config.load_and_validate(invalid("empty.yml"))
        self.assertFalse(result.ok)
        self.assertTrue(result.errors)


class MissingConfigTests(unittest.TestCase):
    def test_missing_file_fails_loud(self):
        result = ralph_config.load_and_validate(os.path.join(FIXTURES, "does-not-exist.yml"))
        self.assertFalse(result.ok)
        joined = "\n".join(result.errors).lower()
        self.assertIn("not found", joined)


class CliTests(unittest.TestCase):
    def _run(self, path):
        return subprocess.run(
            [RALPH, "--check-config", path],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_check_config_exits_zero_for_valid(self):
        proc = self._run(valid("full.yml"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())

    def test_check_config_exits_nonzero_for_invalid(self):
        proc = self._run(invalid("bad-afk-merge.yml"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"afk_merge", proc.stdout)

    def test_check_config_exits_nonzero_for_missing(self):
        proc = self._run(os.path.join(FIXTURES, "does-not-exist.yml"))
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
