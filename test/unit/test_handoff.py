"""Unit tests for Handoff checkpoint + resume (US-008, ADR-0004).

Ralph never compacts context. When an iteration's context fills it writes a
Handoff -- an issue comment (carrying a distinct handoff marker) plus WIP commits
pushed to the story branch -- and terminates cleanly; the next iteration resumes
the same state:in-progress story from that branch with clean context. A
context-full checkpoint is NOT a failed Attempt, so its comment is
distinguishable from an Attempt comment. Resume state lives only in the
superproject and the base branch is never touched (main never touched, ADR-0001).

The deterministic seam mirrors AFK/HIL completion: pure command *plans*
(`handoff_plan` / `resume_plan`) return the ordered git/gh commands without
running anything; a thin runner executes them and the CLI (`ralph --checkpoint`
/ `ralph --resume`) prints and sets exit codes. Behavior is covered against
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
PROMPT_V1 = os.path.join(REPO_ROOT, "prompts", "handoff.v1.md")

sys.path.insert(0, LIB_DIR)
import ralph_handoff  # noqa: E402
import ralph_select  # noqa: E402


def story(number=8, title="Wire up the ADC", type_="afk", state="in-progress"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "type:" + type_}, {"name": "prio:1"},
                   {"name": "state:" + state}],
        "body": "## Acceptance Criteria\n- [ ] does the thing\n\nParent: None\nDepends on: None\n",
        "state": "OPEN",
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


class HandoffPlan(unittest.TestCase):
    def test_persists_wip_and_posts_comment(self):
        plan = ralph_handoff.handoff_plan(story(number=8), "did X, next do Y", base="develop")
        self.assertTrue(plan.ok, plan.errors)
        # WIP is staged, committed, and pushed to the story branch.
        self.assertTrue(any(cmd[:2] == ["git", "add"] for cmd in plan.commands))
        self.assertTrue(any(cmd[:2] == ["git", "commit"] for cmd in plan.commands))
        self.assertTrue(any(cmd[:2] == ["git", "push"] for cmd in plan.commands))
        self.assertIn("ralph/8-wire-up-the-adc", _flat(plan.commands))
        # a Handoff comment is posted to the issue.
        self.assertTrue(any(cmd[:3] == ["gh", "issue", "comment"] for cmd in plan.commands))

    def test_comment_carries_handoff_marker(self):
        plan = ralph_handoff.handoff_plan(story(number=8), "summary", base="develop")
        comment = next(c for c in plan.commands if c[:3] == ["gh", "issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertIn(ralph_handoff.HANDOFF_MARKER, body)
        self.assertIn("summary", body)

    def test_commit_before_push_before_comment(self):
        plan = ralph_handoff.handoff_plan(story(), "s", base="develop")
        kinds = [tuple(cmd[:2]) if cmd[0] == "git" else tuple(cmd[:3]) for cmd in plan.commands]
        self.assertLess(kinds.index(("git", "commit")), kinds.index(("git", "push")))
        self.assertLess(kinds.index(("git", "push")), kinds.index(("gh", "issue", "comment")))

    def test_pushes_story_branch_not_base(self):
        # The base branch is never referenced; only the story branch is pushed.
        plan = ralph_handoff.handoff_plan(story(number=8), "s", base="develop")
        self.assertNotIn("develop", _flat(plan.commands))
        push = next(c for c in plan.commands if c[:2] == ["git", "push"])
        self.assertIn("ralph/8-wire-up-the-adc", push)

    def test_never_touches_main(self):
        plan = ralph_handoff.handoff_plan(story(), "s", base="develop")
        self.assertTrue(plan.ok)
        self.assertNotIn("main", _flat(plan.commands))

    def test_refuses_base_main(self):
        plan = ralph_handoff.handoff_plan(story(), "s", base="main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("main" in e for e in plan.errors))

    def test_refuses_base_main_case_insensitively(self):
        plan = ralph_handoff.handoff_plan(story(), "s", base="Main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])

    def test_honors_custom_branch_pattern(self):
        plan = ralph_handoff.handoff_plan(
            story(number=9), "s", base="develop", branch_pattern="wip/{issue}/{slug}")
        self.assertIn("wip/9/wire-up-the-adc", _flat(plan.commands))


class ResumePlan(unittest.TestCase):
    def test_checks_out_story_branch(self):
        plan = ralph_handoff.resume_plan(story(number=8, state="in-progress"), base="develop")
        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(any(cmd[:2] == ["git", "fetch"] for cmd in plan.commands))
        checkout = next(c for c in plan.commands if c[:2] == ["git", "checkout"])
        self.assertIn("ralph/8-wire-up-the-adc", checkout)

    def test_base_untouched(self):
        plan = ralph_handoff.resume_plan(story(number=8, state="in-progress"), base="develop")
        self.assertNotIn("develop", _flat(plan.commands))

    def test_never_touches_main(self):
        plan = ralph_handoff.resume_plan(story(state="in-progress"), base="develop")
        self.assertNotIn("main", _flat(plan.commands))

    def test_refuses_non_in_progress_story(self):
        plan = ralph_handoff.resume_plan(story(state="ready"), base="develop")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])
        self.assertTrue(any("in-progress" in e for e in plan.errors))

    def test_refuses_base_main(self):
        plan = ralph_handoff.resume_plan(story(state="in-progress"), base="main")
        self.assertFalse(plan.ok)
        self.assertEqual(plan.commands, [])


class HandoffComments(unittest.TestCase):
    """AC#4: a context-full checkpoint is not a failed Attempt -- its comment
    carries the handoff marker so an attempt counter can exclude it."""

    def _attempt(self, body="attempt failed: gating red"):
        return {"body": body}

    def _handoff(self, body="did X"):
        return {"body": ralph_handoff.HANDOFF_MARKER + "\n\n" + body}

    def test_is_handoff_comment_detects_marker(self):
        self.assertTrue(ralph_handoff.is_handoff_comment(self._handoff()))
        self.assertFalse(ralph_handoff.is_handoff_comment(self._attempt()))

    def test_accepts_plain_string_comments(self):
        self.assertTrue(ralph_handoff.is_handoff_comment(ralph_handoff.HANDOFF_MARKER + " x"))
        self.assertFalse(ralph_handoff.is_handoff_comment("just a note"))

    def test_non_handoff_comments_excludes_checkpoints(self):
        comments = [self._attempt("a1"), self._handoff("h1"),
                    self._attempt("a2"), self._handoff("h2")]
        kept = ralph_handoff.non_handoff_comments(comments)
        self.assertEqual(len(kept), 2)
        self.assertFalse(any(ralph_handoff.is_handoff_comment(c) for c in kept))

    def test_latest_handoff_returns_newest(self):
        comments = [self._handoff("old"), self._attempt(), self._handoff("newest")]
        self.assertIn("newest", ralph_handoff.latest_handoff(comments))

    def test_latest_handoff_none_when_absent(self):
        self.assertIsNone(ralph_handoff.latest_handoff([self._attempt()]))


class RunPlan(unittest.TestCase):
    def test_all_ok(self):
        res = ralph_handoff.run_plan([["true"], ["true"]])
        self.assertTrue(res.ok)
        self.assertIsNone(res.failed)

    def test_fail_fast_records_failure(self):
        res = ralph_handoff.run_plan([
            ["true"],
            ["sh", "-c", "echo BOOM; exit 3"],
            ["sh", "-c", "echo SHOULD_NOT_RUN"],
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.failed.returncode, 3)
        self.assertIn("BOOM", res.failed.output)
        self.assertEqual(len(res.steps), 2)


def _mockbin(tmp, git_exit=0, gh_exit=0):
    log = os.path.join(tmp, "calls.log")
    for name, code in (("git", git_exit), ("gh", gh_exit)):
        path = os.path.join(tmp, name)
        with open(path, "w") as fh:
            fh.write('#!/usr/bin/env bash\n'
                     'echo "%s $*" >> "$RALPH_LOG"\n'
                     'exit %d\n' % (name, code))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return log


class CliCheckpoint(unittest.TestCase):
    def _run(self, story_obj, summary, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--checkpoint", "-", summary, config],
            cwd=REPO_ROOT, input=json.dumps(story_obj), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_persists_wip_and_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(number=8), "did X, next Y", config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("commit", calls)
            self.assertIn("push", calls)
            self.assertIn("issue comment", calls)
            self.assertIn(ralph_handoff.HANDOFF_MARKER, calls)
            self.assertNotIn("main", calls)

    def test_refusal_to_touch_main_exits_two_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "base-main.yml")
            proc = self._run(story(), "s", config, tmp, log)
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("main", proc.stderr)
            self.assertFalse(os.path.exists(log))

    def test_gh_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp, gh_exit=1)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(), "s", config, tmp, log)
            self.assertEqual(proc.returncode, 1)

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            log = os.path.join(tmp, "calls.log")
            proc = self._run(story(), "s", config, tmp, log)
            self.assertEqual(proc.returncode, 2)

    def test_missing_summary_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
            proc = subprocess.run(
                [RALPH, "--checkpoint", "-"],
                cwd=REPO_ROOT, input=json.dumps(story()), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(proc.returncode, 2)


class CliResume(unittest.TestCase):
    def _run(self, story_obj, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--resume", "-", config],
            cwd=REPO_ROOT, input=json.dumps(story_obj), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_checks_out_the_story_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(number=8, state="in-progress"), config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("fetch", calls)
            self.assertIn("checkout ralph/8-wire-up-the-adc", calls)
            self.assertNotIn("main", calls)

    def test_prints_latest_handoff_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            s = story(number=8, state="in-progress")
            s["comments"] = [{"body": ralph_handoff.HANDOFF_MARKER + "\n\nresume here please"}]
            proc = self._run(s, config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("resume here please", proc.stdout)

    def test_refuses_non_in_progress_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(state="ready"), config, tmp, log)
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(os.path.exists(log))


class ResumeThroughSelection(unittest.TestCase):
    """AC#2: the selection engine resumes an in-progress checkpointed story
    before scanning ready work -- so a Handoff leaves the story resumable."""

    def test_in_progress_story_is_resumed_first(self):
        checkpointed = story(number=8, state="in-progress")
        ready = story(number=9, state="ready")
        action = ralph_select.next_action([ready, checkpointed])
        self.assertEqual(action.kind, ralph_select.RESUME)
        self.assertEqual(action.number, 8)


class HandoffPromptV1(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(PROMPT_V1), "prompts/handoff.v1.md must be checked in")
        with open(PROMPT_V1) as fh:
            self.text = fh.read()

    def test_covers_the_checkpoint_directives(self):
        low = self.text.lower()
        for needle in ["handoff", "context", "compact", "checkpoint",
                       "terminate", "resume", "attempt", "state:in-progress"]:
            self.assertIn(needle, low, "handoff.v1 prompt missing: %s" % needle)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
