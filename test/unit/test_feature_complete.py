"""Unit tests for Feature completion pass (US-028, ADR-0006).

When a Feature is complete (all stories closed, PRD open + state:ready), the
completion pass integrates the feature branch into base: autosquash-collapse
fixup! commits, rebase onto current base, run full gating, open a single PR,
merge with a merge commit (never squash), and close the PRD.

Failure modes:
  - Autosquash conflict: fall back to uncollapsed linear history, continue
  - Rebase conflict or red gate: push branch, comment on PRD, label
    ready-for-human, stop — NO needs-human circuit breaker
  - Base is main: refuse outright
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
FIXTURES = os.path.join(REPO_ROOT, "test", "fixtures")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_feature  # noqa: E402


def prd_issue(number=18, title="Per-Feature integration branches"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "prd"}, {"name": "state:ready"}],
        "body": "The Feature PRD.\n\nDepends on: None\n",
        "state": "OPEN",
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


# ---------------------------------------------------------------------------
# AC: The happy-path plan runs autosquash, rebase onto base, gating, PR
# create, merge with a merge commit, and PRD close, in that order
# ---------------------------------------------------------------------------
class FeatureCompletePlanHappyPath(unittest.TestCase):

    def _plan(self, **kwargs):
        kwargs.setdefault("base", "develop")
        kwargs.setdefault("feature_pattern", "feature/{issue}-{slug}")
        kwargs.setdefault("gating", [{"name": "test", "run": "make test"}])
        return ralph_feature.feature_complete_plan(prd_issue(), **kwargs)

    def test_plan_is_ok(self):
        plan = self._plan()
        self.assertTrue(plan.ok, plan.errors)

    def test_autosquash_comes_first(self):
        plan = self._plan()
        first = plan.steps[0]
        self.assertEqual(first["name"], "autosquash")
        tokens = _flat(first["commands"])
        self.assertIn("--autosquash", tokens)

    def test_rebase_onto_base_comes_second(self):
        plan = self._plan()
        step = plan.steps[1]
        self.assertEqual(step["name"], "rebase")
        tokens = _flat(step["commands"])
        self.assertIn("develop", tokens)

    def test_gating_comes_third(self):
        plan = self._plan()
        step = plan.steps[2]
        self.assertEqual(step["name"], "gating")

    def test_pr_create_comes_fourth(self):
        plan = self._plan()
        step = plan.steps[3]
        self.assertEqual(step["name"], "pr-create")
        tokens = _flat(step["commands"])
        self.assertIn("pr", tokens)
        self.assertIn("create", tokens)
        self.assertIn("--merge", tokens)

    def test_prd_close_comes_fifth(self):
        plan = self._plan()
        step = plan.steps[4]
        self.assertEqual(step["name"], "prd-close")
        tokens = _flat(step["commands"])
        self.assertIn("issue", tokens)
        self.assertIn("close", tokens)
        self.assertIn("18", tokens)

    def test_merge_uses_merge_commit_never_squash(self):
        plan = self._plan()
        pr_step = plan.steps[3]
        tokens = _flat(pr_step["commands"])
        self.assertIn("--merge", tokens)
        self.assertNotIn("--squash", tokens)
        self.assertNotIn("--rebase", tokens)

    def test_branch_resolves_from_prd(self):
        plan = self._plan()
        self.assertEqual(plan.branch, "feature/18-per-feature-integration-branches")

    def test_step_count(self):
        plan = self._plan()
        self.assertEqual(len(plan.steps), 5)

    def test_never_mentions_main(self):
        plan = self._plan()
        for step in plan.steps:
            self.assertNotIn("main", _flat(step["commands"]))

    def test_honors_custom_feature_pattern(self):
        plan = self._plan(feature_pattern="feat/{issue}/{slug}")
        self.assertEqual(plan.branch, "feat/18/per-feature-integration-branches")


# ---------------------------------------------------------------------------
# AC: An autosquash conflict falls back to the uncollapsed linear history and
# the pass continues
# ---------------------------------------------------------------------------
class AutosquashFallback(unittest.TestCase):

    def test_autosquash_step_has_fallback_flag(self):
        plan = ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])
        autosquash = plan.steps[0]
        self.assertTrue(autosquash.get("fallback_ok"),
                        "autosquash step must tolerate failure (cosmetic fallback)")


# ---------------------------------------------------------------------------
# AC: A rebase conflict or red gate ends with branch pushed, PRD commented and
# labeled ready-for-human, no merge, no needs-human
# ---------------------------------------------------------------------------
class RebaseConflictOrRedGate(unittest.TestCase):

    def _plan(self):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])

    def test_rebase_step_has_blocker_commands(self):
        plan = self._plan()
        rebase = plan.steps[1]
        self.assertIn("on_fail", rebase)
        fail_cmds = rebase["on_fail"]
        tokens = _flat(fail_cmds)
        self.assertIn("push", tokens)
        self.assertIn("ready-for-human", tokens)
        self.assertNotIn("needs-human", tokens)

    def test_gating_step_has_blocker_commands(self):
        plan = self._plan()
        gating = plan.steps[2]
        self.assertIn("on_fail", gating)
        fail_cmds = gating["on_fail"]
        tokens = _flat(fail_cmds)
        self.assertIn("push", tokens)
        self.assertIn("ready-for-human", tokens)
        self.assertNotIn("needs-human", tokens)

    def test_blocker_commands_comment_on_prd(self):
        plan = self._plan()
        for step in plan.steps[1:3]:
            fail_cmds = step["on_fail"]
            self.assertTrue(
                any(c[:3] == ["gh", "issue", "comment"] for c in fail_cmds),
                "%s on_fail must comment on the PRD" % step["name"])

    def test_blocker_commands_label_prd_ready_for_human(self):
        plan = self._plan()
        for step in plan.steps[1:3]:
            fail_cmds = step["on_fail"]
            self.assertTrue(
                any(c[:3] == ["gh", "issue", "edit"] and "ready-for-human" in c
                    for c in fail_cmds),
                "%s on_fail must label PRD ready-for-human" % step["name"])


# ---------------------------------------------------------------------------
# AC: The pass refuses a `main` base
# ---------------------------------------------------------------------------
class RefuseMainBase(unittest.TestCase):

    def test_refuses_main(self):
        plan = ralph_feature.feature_complete_plan(
            prd_issue(), base="main",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])
        self.assertFalse(plan.ok)
        self.assertEqual(plan.steps, [])
        self.assertTrue(any("main" in e for e in plan.errors))

    def test_refuses_main_case_insensitive(self):
        plan = ralph_feature.feature_complete_plan(
            prd_issue(), base="Main",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "make test"}])
        self.assertFalse(plan.ok)


# ---------------------------------------------------------------------------
# Plan runner tests (exercises the step-by-step executor with fallbacks)
# ---------------------------------------------------------------------------
class RunFeaturePlan(unittest.TestCase):

    def _simple_plan(self):
        return ralph_feature.feature_complete_plan(
            prd_issue(), base="develop",
            feature_pattern="feature/{issue}-{slug}",
            gating=[{"name": "test", "run": "true"}])

    def test_happy_path_all_green(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(plan, cwd="/tmp",
                                                 dry_run=True)
        self.assertTrue(result.ok)

    def test_autosquash_conflict_continues(self):
        """Autosquash failure is cosmetic; the pass continues."""
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"autosquash"})
        self.assertTrue(result.ok)

    def test_rebase_conflict_stops_and_runs_on_fail(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"rebase"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "rebase")
        self.assertTrue(result.on_fail_ran)

    def test_gating_failure_stops_and_runs_on_fail(self):
        plan = self._simple_plan()
        result = ralph_feature.run_feature_plan(
            plan, cwd="/tmp", dry_run=True,
            inject_failures={"gating"})
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step, "gating")
        self.assertTrue(result.on_fail_ran)


# ---------------------------------------------------------------------------
# CLI smoke tests against mocked git/gh
# ---------------------------------------------------------------------------
class CliCompleteFeature(unittest.TestCase):

    def _mockbin(self, tmp, git_exit=0, gh_exit=0):
        log = os.path.join(tmp, "calls.log")
        for name, code in (("git", git_exit), ("gh", gh_exit), ("make", 0)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s $*" >> "$RALPH_LOG"\n'
                         'exit %d\n' % (name, code))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return log

    def _run(self, prd, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        prd_path = os.path.join(tmp, "prd.json")
        with open(prd_path, "w") as fh:
            json.dump(prd, fh)
        return subprocess.run(
            [RALPH, "--complete-feature", prd_path, config],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_happy_path_merges_and_closes_prd(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(prd_issue(), config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("pr merge", calls)
            self.assertIn("issue close 18", calls)
            self.assertNotIn("main", calls)

    def test_refuses_main_base_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "base-main.yml")
            proc = self._run(prd_issue(), config, tmp, log)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("main", proc.stderr)
            self.assertFalse(os.path.exists(log))


if __name__ == "__main__":
    unittest.main()
