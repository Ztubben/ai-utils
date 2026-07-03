"""Unit tests for the canonical Ralph story-format validator (US-003).

The ralph-story formatting skill emits GitHub issues in a canonical
label + body shape. This validates that shape so example runs produce
well-formed issues the selection engine (US-004) can consume, and so a
human can lint skill output with `ralph --lint-story`.
"""
import glob
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
INVALID = os.path.join(REPO_ROOT, "test", "fixtures", "stories", "invalid")
EXAMPLES = os.path.join(REPO_ROOT, "skills", "ralph-story", "examples")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_story  # noqa: E402


def load(path):
    with open(path) as fh:
        return json.load(fh)


def invalid(name):
    return load(os.path.join(INVALID, name))


def example(name):
    return load(os.path.join(EXAMPLES, name))


class ExampleStoriesAreCanonical(unittest.TestCase):
    def test_every_shipped_example_validates(self):
        paths = sorted(glob.glob(os.path.join(EXAMPLES, "*.json")))
        self.assertTrue(paths, "the skill must ship example issues")
        for path in paths:
            result = ralph_story.validate_story(load(path))
            self.assertTrue(result.ok, "%s: %s" % (os.path.basename(path), result.errors))

    def test_afk_example_is_classified_afk(self):
        result = ralph_story.validate_story(example("afk-story.json"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.fields["type"], "afk")
        self.assertFalse(result.fields["is_blocker"])

    def test_hil_example_is_classified_hil_with_bench(self):
        result = ralph_story.validate_story(example("hil-story.json"))
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.fields["type"], "hil")
        self.assertTrue(result.fields["has_bench"])

    def test_hil_example_depends_on_is_parsed(self):
        result = ralph_story.validate_story(example("hil-story.json"))
        self.assertEqual(result.fields["depends_on"], [42])

    def test_afk_example_depends_on_none(self):
        result = ralph_story.validate_story(example("afk-story.json"))
        self.assertEqual(result.fields["depends_on"], [])

    def test_blocker_example_is_a_blocker_kept_out_of_ready(self):
        result = ralph_story.validate_story(example("blocker.json"))
        self.assertTrue(result.ok, result.errors)
        self.assertTrue(result.fields["is_blocker"])
        self.assertNotEqual(result.fields["state"], "ready")


class MalformedStoriesAreRejected(unittest.TestCase):
    def _assert_invalid_mentioning(self, fixture, needle):
        result = ralph_story.validate_story(invalid(fixture))
        self.assertFalse(result.ok, "expected %s to be rejected" % fixture)
        joined = "\n".join(result.errors)
        self.assertIn(needle, joined,
                      "expected an error mentioning %r, got: %s" % (needle, joined))

    def test_missing_acceptance_is_rejected(self):
        self._assert_invalid_mentioning("missing-acceptance.json", "Acceptance Criteria")

    def test_hitl_spelling_is_rejected(self):
        # Terminology is standardized on HIL, not HITL.
        self._assert_invalid_mentioning("hitl-typo.json", "HITL")

    def test_hil_without_bench_procedure_is_rejected(self):
        self._assert_invalid_mentioning("hil-missing-bench.json", "Bench Test Procedure")

    def test_two_type_labels_are_rejected(self):
        self._assert_invalid_mentioning("two-types.json", "type")

    def test_two_prio_labels_are_rejected(self):
        # prio is optional, but at most one: two prio:N labels are ambiguous.
        self._assert_invalid_mentioning("two-prios.json", "prio")

    def test_missing_depends_on_is_rejected(self):
        self._assert_invalid_mentioning("missing-depends.json", "Depends on")

    def test_blocker_in_ready_is_rejected(self):
        self._assert_invalid_mentioning("blocker-ready.json", "state:ready")


class LabelShapeIsFlexible(unittest.TestCase):
    def test_plain_string_labels_are_accepted(self):
        story = {
            "number": 1,
            "title": "String labels",
            "labels": ["state:ready", "type:afk", "prio:3"],
            "body": "## Acceptance Criteria\n- [ ] ok\n\nDepends on: None\n",
        }
        result = ralph_story.validate_story(story)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.fields["prio"], 3)


class PrioIsOptional(unittest.TestCase):
    def _story(self, labels):
        return {
            "number": 1, "title": "s", "labels": labels,
            "body": "## Acceptance Criteria\n- [ ] ok\n\nDepends on: None\n",
        }

    def test_no_prio_label_is_valid_with_none_prio(self):
        # A story may omit prio:N; it validates and carries prio=None.
        result = ralph_story.validate_story(
            self._story([{"name": "state:ready"}, {"name": "type:afk"}]))
        self.assertTrue(result.ok, result.errors)
        self.assertIsNone(result.fields["prio"])

    def test_non_numeric_prio_is_rejected(self):
        result = ralph_story.validate_story(
            self._story([{"name": "state:ready"}, {"name": "type:afk"},
                         {"name": "prio:high"}]))
        self.assertFalse(result.ok)
        self.assertTrue(any("prio" in e for e in result.errors))


class CliLintStory(unittest.TestCase):
    def _run(self, path):
        return subprocess.run(
            [RALPH, "--lint-story", path],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_lint_story_exits_zero_for_valid_example(self):
        proc = self._run(os.path.join(EXAMPLES, "afk-story.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())

    def test_lint_story_exits_nonzero_for_malformed(self):
        proc = self._run(os.path.join(INVALID, "hitl-typo.json"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"HITL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
