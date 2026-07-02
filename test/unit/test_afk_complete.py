"""Unit tests for AFK completion (US-006, ADR-0001).

For a green type:afk story Ralph auto-merges the story branch into base per the
afk_merge policy and closes the issue (marks it Passing), never touching main.
The deterministic seam is a pure command *plan* (the ordered git/gh commands);
a thin runner executes it, and the CLI (`ralph --complete-afk`) prints + sets
exit codes. Behavior is covered against mocked git/gh on PATH.
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
import ralph_afk  # noqa: E402
import ralph_select  # noqa: E402


def afk_story(number=6, title="Add SPI driver", type_="afk"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "type:" + type_}, {"name": "prio:1"},
                   {"name": "state:in-progress"}],
        "body": "## Acceptance Criteria\n- [ ] does the thing\n\nDepends on: None\n"
                + ("\n## Bench Test Procedure\n- poke it\n" if type_ == "hil" else ""),
        "state": "OPEN",
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


class AfkCompletePlan(unittest.TestCase):
    def test_merges_into_base_and_closes_issue(self):
        plan = ralph_afk.afk_complete_plan(afk_story(number=6), base="develop")
        self.assertTrue(plan.ok, plan.errors)
        tokens = _flat(plan.commands)
        self.assertIn("develop", tokens)
        # the story branch is what gets merged
        self.assertIn("ralph/6-add-spi-driver", tokens)
        # the issue is closed after merge
        self.assertTrue(any(cmd[:3] == ["gh", "issue", "close"] for cmd in plan.commands))

    def test_never_touches_main(self):
        plan = ralph_afk.afk_complete_plan(afk_story(), base="develop")
        self.assertTrue(plan.ok)
        self.assertNotIn("main", _flat(plan.commands))

    def test_merge_method_maps_afk_merge(self):
        for method, flag in (("merge", "--merge"), ("squash", "--squash"),
                             ("rebase", "--rebase")):
            plan = ralph_afk.afk_complete_plan(afk_story(), base="develop",
                                               afk_merge=method)
            self.assertTrue(plan.ok, plan.errors)
            merge = next(c for c in plan.commands if c[:3] == ["gh", "pr", "merge"])
            self.assertIn(flag, merge)
            # exactly the one method flag, no other merge-method flag leaks in
            others = {"--merge", "--squash", "--rebase"} - {flag}
            self.assertFalse(others & set(merge))

    def test_close_comes_after_merge(self):
        plan = ralph_afk.afk_complete_plan(afk_story(), base="develop")
        merge_i = next(i for i, c in enumerate(plan.commands)
                       if c[:3] == ["gh", "pr", "merge"])
        close_i = next(i for i, c in enumerate(plan.commands)
                       if c[:3] == ["gh", "issue", "close"])
        self.assertLess(merge_i, close_i)

    def test_pr_links_the_issue_so_it_closes(self):
        plan = ralph_afk.afk_complete_plan(afk_story(number=42), base="develop")
        self.assertIn("Closes #42", _flat(plan.commands))

    def test_refuses_to_merge_into_main(self):
        plan = ralph_afk.afk_complete_plan(afk_story(), base="main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("main" in e for e in plan.errors))

    def test_refuses_main_case_insensitively(self):
        plan = ralph_afk.afk_complete_plan(afk_story(), base="Main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_refuses_non_afk_story(self):
        plan = ralph_afk.afk_complete_plan(afk_story(type_="hil"), base="develop")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("afk" in e.lower() for e in plan.errors))

    def test_honors_custom_branch_pattern(self):
        plan = ralph_afk.afk_complete_plan(
            afk_story(number=9), base="develop", branch_pattern="wip/{issue}/{slug}")
        self.assertIn("wip/9/add-spi-driver", _flat(plan.commands))


class RunPlan(unittest.TestCase):
    def test_all_ok(self):
        res = ralph_afk.run_plan([["true"], ["true"]])
        self.assertTrue(res.ok)
        self.assertIsNone(res.failed)
        self.assertEqual(len(res.steps), 2)

    def test_fail_fast_records_failure(self):
        res = ralph_afk.run_plan([
            ["true"],
            ["sh", "-c", "echo BOOM; exit 3"],
            ["sh", "-c", "echo SHOULD_NOT_RUN"],
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.failed.returncode, 3)
        self.assertIn("BOOM", res.failed.output)
        self.assertEqual(len(res.steps), 2)  # third never runs


class CliCompleteAfk(unittest.TestCase):
    def _mockbin(self, tmp, git_exit=0, gh_exit=0):
        """Create mock git/gh on a temp dir; each logs its argv to $RALPH_LOG."""
        log = os.path.join(tmp, "calls.log")
        for name, code in (("git", git_exit), ("gh", gh_exit)):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s $*" >> "$RALPH_LOG"\n'
                         'exit %d\n' % (name, code))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return log

    def _run(self, story, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--complete-afk", "-", config],
            cwd=REPO_ROOT, input=json.dumps(story), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_merges_and_closes_via_mocked_gh(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(afk_story(number=6), config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("pr merge", calls)
            self.assertIn("issue close 6", calls)
            self.assertNotIn("main", calls)

    def test_refusal_to_touch_main_exits_two_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "base-main.yml")
            proc = self._run(afk_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("main", proc.stderr)
            self.assertFalse(os.path.exists(log))  # nothing executed

    def test_gh_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp, gh_exit=1)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(afk_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 1)

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            log = os.path.join(tmp, "calls.log")
            proc = self._run(afk_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 2)


class DependentsUnblockedWhenClosed(unittest.TestCase):
    """AC#5: a merged (hence closed) AFK story satisfies dependents' edges."""

    def _dep(self, number, closed):
        return {
            "number": number, "title": "dep",
            "labels": [{"name": "type:afk"}, {"name": "prio:1"},
                       {"name": "state:blocked"}],
            "body": "## Acceptance Criteria\n- [ ] x\n\nDepends on: None\n",
            "state": "CLOSED" if closed else "OPEN",
        }

    def _dependent(self):
        return {
            "number": 20, "title": "dependent",
            "labels": [{"name": "type:afk"}, {"name": "prio:1"},
                       {"name": "state:ready"}],
            "body": "## Acceptance Criteria\n- [ ] x\n\nDepends on: #10\n",
            "state": "OPEN",
        }

    def test_open_dep_blocks_and_closed_dep_unblocks(self):
        before = ralph_select.next_action([self._dep(10, closed=False), self._dependent()])
        self.assertEqual(before.kind, ralph_select.NO_WORK)
        after = ralph_select.next_action([self._dep(10, closed=True), self._dependent()])
        self.assertEqual(after.kind, ralph_select.START)
        self.assertEqual(after.number, 20)


if __name__ == "__main__":
    unittest.main()
