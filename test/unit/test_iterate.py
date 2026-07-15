"""Unit tests for the single-iteration mechanics (US-005, ADR-0003).

An iteration takes a chosen story and drives it TDD off-target to a green local
gate. The deterministic seams are pure/host-testable: computing the story branch
name from `branch_pattern`, and running the configured gating steps locally with
low-verbosity output. The judgment-heavy TDD itself is driven by the checked-in
agent prompt v1, whose required directives are drift-guarded here.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures")
GATING_FIX = os.path.join(FIXTURES, "gating")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")
PROMPT_V1 = os.path.join(REPO_ROOT, "prompts", "iterate.v1.md")

sys.path.insert(0, LIB_DIR)
import ralph_iterate  # noqa: E402


class Slugify(unittest.TestCase):
    def test_lowercases_and_dashes_non_alnum(self):
        self.assertEqual(ralph_iterate.slugify("Add SPI driver"), "add-spi-driver")

    def test_collapses_and_strips_dashes(self):
        self.assertEqual(ralph_iterate.slugify("  Foo -- Bar!! "), "foo-bar")

    def test_truncates_long_titles_without_trailing_dash(self):
        slug = ralph_iterate.slugify("word " * 40)
        self.assertLessEqual(len(slug), 50)
        self.assertFalse(slug.endswith("-"))


class BranchName(unittest.TestCase):
    def test_default_pattern_substitutes_issue_and_slug(self):
        story = {"number": 5, "title": "Add SPI driver for the sensor"}
        self.assertEqual(
            ralph_iterate.branch_name(story),
            "ralph/5-add-spi-driver-for-the-sensor",
        )

    def test_honors_custom_pattern(self):
        story = {"number": 12, "title": "Do a thing"}
        self.assertEqual(
            ralph_iterate.branch_name(story, "wip/{issue}/{slug}"),
            "wip/12/do-a-thing",
        )

    def test_default_pattern_matches_schema_default(self):
        self.assertEqual(ralph_iterate.DEFAULT_BRANCH_PATTERN, "ralph/{issue}-{slug}")


class ResolveBranch(unittest.TestCase):
    """Working-branch resolution per story kind (ADR-0006).

    An Orphan Story works on its own story branch (branch_pattern); a Feature
    story works directly on its Feature's integration branch (feature_pattern),
    named from the PRD issue's number and title.
    """

    ORPHAN = {"number": 7, "title": "Wire up the ADC",
              "body": "Parent: None\nDepends on: None\n"}
    FEATURE_STORY = {"number": 24, "title": "Working-branch resolution",
                     "body": "Parent: #18\nDepends on: None\n"}
    PRD = {"number": 18, "title": "Per-Feature integration branches",
           "body": "Depends on: None\n"}

    def test_orphan_story_resolves_to_branch_pattern(self):
        self.assertEqual(
            ralph_iterate.resolve_branch(self.ORPHAN),
            "ralph/7-wire-up-the-adc",
        )

    def test_orphan_resolution_matches_legacy_branch_name(self):
        self.assertEqual(
            ralph_iterate.resolve_branch(self.ORPHAN),
            ralph_iterate.branch_name(self.ORPHAN),
        )

    def test_feature_story_resolves_to_prd_feature_branch(self):
        self.assertEqual(
            ralph_iterate.resolve_branch(self.FEATURE_STORY, prd=self.PRD),
            "feature/18-per-feature-integration-branches",
        )

    def test_feature_branch_honors_custom_patterns(self):
        self.assertEqual(
            ralph_iterate.resolve_branch(
                self.FEATURE_STORY, prd=self.PRD,
                feature_pattern="feat/{issue}/{slug}"),
            "feat/18/per-feature-integration-branches",
        )

    def test_feature_slug_truncated_to_50_chars_from_prd_title(self):
        prd = dict(self.PRD, title="word " * 40)
        name = ralph_iterate.resolve_branch(self.FEATURE_STORY, prd=prd)
        slug = name[len("feature/18-"):]
        self.assertLessEqual(len(slug), 50)
        self.assertFalse(slug.endswith("-"))

    def test_resolution_is_deterministic(self):
        first = ralph_iterate.resolve_branch(self.FEATURE_STORY, prd=self.PRD)
        for _ in range(3):
            self.assertEqual(
                ralph_iterate.resolve_branch(self.FEATURE_STORY, prd=self.PRD),
                first,
            )

    def test_feature_story_without_prd_context_is_an_error(self):
        with self.assertRaises(ValueError):
            ralph_iterate.resolve_branch(self.FEATURE_STORY)

    def test_prd_number_must_match_parent(self):
        wrong = dict(self.PRD, number=99)
        with self.assertRaises(ValueError):
            ralph_iterate.resolve_branch(self.FEATURE_STORY, prd=wrong)

    def test_default_feature_pattern_matches_schema_default(self):
        self.assertEqual(ralph_iterate.DEFAULT_FEATURE_PATTERN, "feature/{issue}-{slug}")


class RunGating(unittest.TestCase):
    def test_all_passing_steps_are_ok(self):
        res = ralph_iterate.run_gating(
            [{"name": "build", "run": "true"}, {"name": "test", "run": "true"}]
        )
        self.assertTrue(res.ok)
        self.assertIsNone(res.failed)
        self.assertEqual(len(res.steps), 2)

    def test_stops_at_first_failure_and_records_it(self):
        res = ralph_iterate.run_gating([
            {"name": "first", "run": "true"},
            {"name": "boom", "run": "echo BOOM; exit 3"},
            {"name": "never", "run": "echo SHOULD_NOT_RUN"},
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.failed.name, "boom")
        self.assertEqual(res.failed.returncode, 3)
        self.assertIn("BOOM", res.failed.output)
        # fail-fast: the third step is never attempted.
        self.assertEqual([s.name for s in res.steps], ["first", "boom"])

    def test_captures_stderr_into_output(self):
        res = ralph_iterate.run_gating([{"name": "x", "run": "echo oops >&2; exit 1"}])
        self.assertIn("oops", res.failed.output)


class CliBranchName(unittest.TestCase):
    def _run(self, story, *extra):
        return subprocess.run(
            [RALPH, "--branch-name", "-", *extra],
            cwd=REPO_ROOT, input=json.dumps(story),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def test_prints_default_branch_for_a_story(self):
        proc = self._run({"number": 7, "title": "Wire up the ADC"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ralph/7-wire-up-the-adc")

    def test_uses_branch_pattern_from_config(self):
        proc = self._run({"number": 7, "title": "Wire up the ADC"},
                         os.path.join(FIXTURES, "config", "valid", "full.yml"))
        self.assertEqual(proc.returncode, 0, proc.stdout)
        # full.yml carries the default pattern; assert it resolves through config.
        self.assertEqual(proc.stdout.strip(), "ralph/7-wire-up-the-adc")

    def test_orphan_story_with_parent_none_resolves_unchanged(self):
        proc = self._run({"number": 7, "title": "Wire up the ADC",
                          "body": "Parent: None\nDepends on: None\n"})
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(), "ralph/7-wire-up-the-adc")

    def _write_json(self, data):
        fh = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8")
        self.addCleanup(os.unlink, fh.name)
        json.dump(data, fh)
        fh.close()
        return fh.name

    def test_feature_story_resolves_via_prd_json(self):
        prd_path = self._write_json(
            {"number": 18, "title": "Per-Feature integration branches",
             "body": "Depends on: None\n"})
        proc = self._run({"number": 24, "title": "Working-branch resolution",
                          "body": "Parent: #18\nDepends on: None\n"},
                         "", prd_path)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(),
                         "feature/18-per-feature-integration-branches")

    def test_feature_story_with_config_uses_feature_pattern(self):
        prd_path = self._write_json(
            {"number": 18, "title": "Per-Feature integration branches",
             "body": "Depends on: None\n"})
        proc = self._run({"number": 24, "title": "Working-branch resolution",
                          "body": "Parent: #18\nDepends on: None\n"},
                         os.path.join(FIXTURES, "config", "valid", "full.yml"),
                         prd_path)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout.strip(),
                         "feature/18-per-feature-integration-branches")

    def test_feature_story_without_prd_context_exits_two(self):
        proc = self._run({"number": 24, "title": "Working-branch resolution",
                          "body": "Parent: #18\nDepends on: None\n"})
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("#18", proc.stdout)

    def test_prd_mismatching_parent_exits_two(self):
        prd_path = self._write_json(
            {"number": 99, "title": "Wrong PRD", "body": "Depends on: None\n"})
        proc = self._run({"number": 24, "title": "Working-branch resolution",
                          "body": "Parent: #18\nDepends on: None\n"},
                         "", prd_path)
        self.assertEqual(proc.returncode, 2, proc.stdout)


class CliRunGating(unittest.TestCase):
    def _run(self, config):
        return subprocess.run(
            [RALPH, "--run-gating", config],
            cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_passing_gating_exits_zero(self):
        proc = self._run(os.path.join(GATING_FIX, "pass.yml"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # low-verbosity: passing steps show only a check line, no step output.
        self.assertNotIn("SHOULD_NOT_APPEAR", proc.stdout)

    def test_failing_gating_exits_nonzero_and_shows_failed_output(self):
        proc = self._run(os.path.join(GATING_FIX, "fail.yml"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("BOOM", proc.stderr)
        # fail-fast: a step after the failing one must not have run.
        self.assertNotIn("SHOULD_NOT_RUN", proc.stdout + proc.stderr)

    def test_invalid_config_exits_two(self):
        proc = self._run(os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml"))
        self.assertEqual(proc.returncode, 2)


class AgentPromptV1(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(PROMPT_V1), "prompts/iterate.v1.md must be checked in")
        with open(PROMPT_V1) as fh:
            self.text = fh.read()

    def test_covers_the_iteration_directives(self):
        low = self.text.lower()
        for needle in ["red", "green", "hal", "off-target", "acceptance criteria",
                       "gating", "{issue}", "{slug}"]:
            self.assertIn(needle, low, "iterate.v1 prompt missing: %s" % needle)

    def test_forbids_touching_base_and_main(self):
        low = self.text.lower()
        self.assertIn("main", low)
        self.assertIn("never", low)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)

    # --- Feature-workflow directives (ADR-0006, #31) ---

    def test_branch_step_covers_both_story_kinds(self):
        """Branch step must mention both Orphan Story and Feature story paths."""
        low = self.text.lower()
        self.assertIn("orphan", low,
                       "branch step must mention Orphan stories")
        self.assertIn("feature", low,
                       "branch step must mention Feature stories")

    def test_branch_step_resolves_via_shipped_cli(self):
        """Branch resolution must go through the shipped CLI for both kinds."""
        self.assertIn("ralph --branch-name", self.text,
                       "branch step must use `ralph --branch-name` CLI")

    def test_hard_sync_from_origin_before_work(self):
        """Prompt must instruct a hard sync from origin before starting work."""
        low = self.text.lower()
        self.assertIn("hard-sync", low,
                       "prompt must instruct hard-sync from origin")
        self.assertIn("origin", low,
                       "prompt must reference origin for hard-sync")

    def test_fixup_repair_for_bench_failed_stories(self):
        """Bench-failed HIL stories must be repaired with fixup! commits."""
        self.assertIn("fixup!", self.text,
                       "prompt must mandate fixup! commits for bench-failed repairs")

    def test_forbids_history_rewriting_by_iterations(self):
        """Iterations must never rewrite history (rebase, amend, force-push)."""
        low = self.text.lower()
        self.assertIn("never rewrite history", low,
                       "prompt must explicitly forbid history rewriting")

    def test_existing_guardrails_preserved(self):
        """Existing scope guardrails and done-signal must remain."""
        self.assertIn("RALPH-STORY-COMPLETE", self.text)
        low = self.text.lower()
        # Markdown-bold markers are kept; match without them.
        self.assertRegex(low, r"never.*merge into the base branch")
        self.assertIn("do not close the issue", low)


if __name__ == "__main__":
    unittest.main()
