"""Unit tests for HIL completion (US-007, ADR-0001, ADR-0003).

For a green type:hil story Ralph opens a PR to base and moves the issue to
state:awaiting-bench, then STOPS -- it never merges a HIL story and never closes
the issue, so the human bench-tests one clean diff in isolation. A HIL story in
state:awaiting-bench is not Passing and does not satisfy dependents' Depends on:
edges (the issue stays open). The deterministic seam is a pure command *plan*
(the ordered git/gh commands); a thin runner executes it, and the CLI
(`ralph --complete-hil`) prints + sets exit codes. Behavior is covered against
mocked git/gh on PATH.
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
import ralph_hil  # noqa: E402
import ralph_select  # noqa: E402


def hil_story(number=7, title="Blink status LED", type_="hil"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "type:" + type_}, {"name": "prio:1"},
                   {"name": "state:in-progress"}],
        "body": "## Acceptance Criteria\n- [ ] does the thing\n\nParent: None\nDepends on: None\n"
                + ("\n## Bench Test Procedure\n- poke it\n" if type_ == "hil" else ""),
        "state": "OPEN",
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


class HilCompletePlan(unittest.TestCase):
    def test_opens_pr_to_base(self):
        plan = ralph_hil.hil_complete_plan(hil_story(number=7), base="develop")
        self.assertTrue(plan.ok, plan.errors)
        tokens = _flat(plan.commands)
        self.assertIn("develop", tokens)
        self.assertIn("ralph/7-blink-status-led", tokens)
        # a PR is created off base
        self.assertTrue(any(cmd[:3] == ["gh", "pr", "create"] for cmd in plan.commands))
        create = next(c for c in plan.commands if c[:3] == ["gh", "pr", "create"])
        self.assertIn("--base", create)
        self.assertIn("develop", create)

    def test_moves_to_awaiting_bench(self):
        plan = ralph_hil.hil_complete_plan(hil_story(number=7), base="develop")
        edit = next(c for c in plan.commands if c[:3] == ["gh", "issue", "edit"])
        self.assertIn("state:awaiting-bench", edit)
        # the in-progress workflow label is removed
        self.assertIn("state:in-progress", edit)
        i = edit.index("state:in-progress")
        self.assertEqual(edit[i - 1], "--remove-label")
        j = edit.index("state:awaiting-bench")
        self.assertEqual(edit[j - 1], "--add-label")

    def test_never_merges(self):
        plan = ralph_hil.hil_complete_plan(hil_story(), base="develop")
        self.assertTrue(plan.ok)
        self.assertFalse(any(cmd[:3] == ["gh", "pr", "merge"] for cmd in plan.commands))

    def test_never_closes_the_issue(self):
        """The issue stays open so it does not (yet) satisfy dependents (AC#4)."""
        plan = ralph_hil.hil_complete_plan(hil_story(number=7), base="develop")
        self.assertFalse(any(cmd[:3] == ["gh", "issue", "close"] for cmd in plan.commands))
        # the PR must not auto-close the issue on merge either
        self.assertNotIn("Closes #7", _flat(plan.commands))

    def test_never_touches_main(self):
        plan = ralph_hil.hil_complete_plan(hil_story(), base="develop")
        self.assertTrue(plan.ok)
        self.assertNotIn("main", _flat(plan.commands))

    def test_pr_references_the_issue(self):
        plan = ralph_hil.hil_complete_plan(hil_story(number=42), base="develop")
        self.assertTrue(any("#42" in tok for tok in _flat(plan.commands)))

    def test_pr_before_label_move(self):
        plan = ralph_hil.hil_complete_plan(hil_story(), base="develop")
        create_i = next(i for i, c in enumerate(plan.commands)
                        if c[:3] == ["gh", "pr", "create"])
        edit_i = next(i for i, c in enumerate(plan.commands)
                      if c[:3] == ["gh", "issue", "edit"])
        self.assertLess(create_i, edit_i)

    def test_refuses_to_open_pr_into_main(self):
        plan = ralph_hil.hil_complete_plan(hil_story(), base="main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("main" in e for e in plan.errors))

    def test_refuses_main_case_insensitively(self):
        plan = ralph_hil.hil_complete_plan(hil_story(), base="Main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_refuses_non_hil_story(self):
        plan = ralph_hil.hil_complete_plan(hil_story(type_="afk"), base="develop")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("hil" in e.lower() for e in plan.errors))

    def test_honors_custom_branch_pattern(self):
        plan = ralph_hil.hil_complete_plan(
            hil_story(number=9), base="develop", branch_pattern="wip/{issue}/{slug}")
        self.assertIn("wip/9/blink-status-led", _flat(plan.commands))

    def test_push_uses_head_so_local_branch_name_need_not_match(self):
        # Promotion must not depend on the iteration's local branch carrying the
        # exact canonical name: push the current HEAD to the canonical remote
        # branch, while the PR head still references that canonical name.
        plan = ralph_hil.hil_complete_plan(hil_story(number=7), base="develop")
        push = next(c for c in plan.commands if c[:2] == ["git", "push"])
        self.assertIn("HEAD:ralph/7-blink-status-led", push)
        self.assertNotIn("ralph/7-blink-status-led", push)  # bare name is not the src refspec
        create = next(c for c in plan.commands if c[:3] == ["gh", "pr", "create"])
        self.assertIn("ralph/7-blink-status-led", create)


class RunPlan(unittest.TestCase):
    def test_all_ok(self):
        res = ralph_hil.run_plan([["true"], ["true"]])
        self.assertTrue(res.ok)
        self.assertIsNone(res.failed)

    def test_fail_fast_records_failure(self):
        res = ralph_hil.run_plan([
            ["true"],
            ["sh", "-c", "echo BOOM; exit 3"],
            ["sh", "-c", "echo SHOULD_NOT_RUN"],
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.failed.returncode, 3)
        self.assertIn("BOOM", res.failed.output)
        self.assertEqual(len(res.steps), 2)  # third never runs


class CliCompleteHil(unittest.TestCase):
    def _mockbin(self, tmp, git_exit=0, gh_exit=0):
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
            [RALPH, "--complete-hil", "-", config],
            cwd=REPO_ROOT, input=json.dumps(story), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_opens_pr_and_moves_to_awaiting_bench(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(hil_story(number=7), config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("pr create", calls)
            self.assertIn("state:awaiting-bench", calls)
            self.assertNotIn("pr merge", calls)
            self.assertNotIn("issue close", calls)
            self.assertNotIn("main", calls)

    def test_refusal_to_touch_main_exits_two_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "base-main.yml")
            proc = self._run(hil_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("main", proc.stderr)
            self.assertFalse(os.path.exists(log))

    def test_gh_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp, gh_exit=1)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(hil_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 1)

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            log = os.path.join(tmp, "calls.log")
            proc = self._run(hil_story(), config, tmp, log)
            self.assertEqual(proc.returncode, 2)

    def test_refuses_afk_story_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(hil_story(type_="afk"), config, tmp, log)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(log))


class AwaitingBenchDoesNotSatisfyDependents(unittest.TestCase):
    """AC#4: a HIL story parked at state:awaiting-bench is not Passing (stays
    open) and does not satisfy a dependent's Depends on: edge; only once it is
    bench-verified (closed) does the dependent become eligible."""

    def _hil_dep(self, number, closed):
        return {
            "number": number, "title": "hil dep",
            "labels": [{"name": "type:hil"}, {"name": "prio:1"},
                       {"name": "state:awaiting-bench"}],
            "body": "## Acceptance Criteria\n- [ ] x\n\nParent: None\nDepends on: None\n"
                    "\n## Bench Test Procedure\n- poke it\n",
            "state": "CLOSED" if closed else "OPEN",
        }

    def _dependent(self):
        return {
            "number": 20, "title": "dependent",
            "labels": [{"name": "type:afk"}, {"name": "prio:1"},
                       {"name": "state:ready"}],
            "body": "## Acceptance Criteria\n- [ ] x\n\nParent: None\nDepends on: #10\n",
            "state": "OPEN",
        }

    def test_awaiting_bench_blocks_and_verified_unblocks(self):
        before = ralph_select.next_action([self._hil_dep(10, closed=False), self._dependent()])
        self.assertEqual(before.kind, ralph_select.NO_WORK)
        after = ralph_select.next_action([self._hil_dep(10, closed=True), self._dependent()])
        self.assertEqual(after.kind, ralph_select.START)
        self.assertEqual(after.number, 20)


if __name__ == "__main__":
    unittest.main()
