"""Unit tests for failure handling + circuit breaker + needs-human (US-009, ADR-0004).

An **Attempt** is an iteration that ends without the story reaching green. After
`limits.max_attempts` (default 3) failed Attempts a story moves to state:blocked
with one terse comment; a context-full checkpoint (Handoff) is NOT an Attempt.
When a second story also blocks, `limits.circuit_breaker` (default 2) trips: the
loop halts, the `needs-human` label is applied and the configured handle tagged.

The deterministic seams mirror the completion stages: pure command *plans*
(`attempt_plan` / `circuit_breaker_plan`) return the ordered gh commands without
running anything; a thin runner executes them and the CLI (`ralph --record-attempt`
/ `ralph --check-breaker`) prints and sets exit codes. Attempt counting is built on
`ralph_handoff.non_handoff_comments` so a checkpoint never counts as an Attempt.
Behavior is covered against mocked gh on PATH.
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
PROMPT_V1 = os.path.join(REPO_ROOT, "prompts", "failure.v1.md")

sys.path.insert(0, LIB_DIR)
import ralph_failure  # noqa: E402
import ralph_handoff  # noqa: E402
import ralph_iterate  # noqa: E402
import ralph_select  # noqa: E402


def story(number=9, title="Add SPI driver", type_="afk", state="in-progress",
          comments=None):
    s = {
        "number": number,
        "title": title,
        "labels": [{"name": "type:" + type_}, {"name": "prio:1"},
                   {"name": "state:" + state}],
        "body": "## Acceptance Criteria\n- [ ] does the thing\n\nParent: None\nDepends on: None\n"
                + ("\n## Bench Test Procedure\n- poke it\n" if type_ == "hil" else ""),
        "state": "OPEN",
    }
    if comments is not None:
        s["comments"] = comments
    return s


def feature_story(number=9, parent=18, **kwargs):
    s = story(number=number, **kwargs)
    s["body"] = s["body"].replace("Parent: None", "Parent: #%d" % parent)
    return s


def prd_issue(number=18, title="Per-Feature integration branches"):
    return {
        "number": number,
        "title": title,
        "labels": [{"name": "prd"}, {"name": "state:ready"}],
        "body": "The Feature PRD.",
        "state": "OPEN",
    }


def _flat(commands):
    return [tok for cmd in commands for tok in cmd]


def _attempt_comment(body="gating red"):
    return {"body": ralph_failure.ATTEMPT_MARKER + "\n\n" + body}


def _handoff_comment(body="did X"):
    return {"body": ralph_handoff.HANDOFF_MARKER + "\n\n" + body}


class CountAttempts(unittest.TestCase):
    def test_zero_when_no_comments(self):
        self.assertEqual(ralph_failure.count_attempts([]), 0)
        self.assertEqual(ralph_failure.count_attempts(None), 0)

    def test_counts_attempt_marked_comments(self):
        comments = [_attempt_comment("a1"), _attempt_comment("a2")]
        self.assertEqual(ralph_failure.count_attempts(comments), 2)

    def test_excludes_handoff_checkpoints(self):
        # AC#1: a context-full checkpoint is NOT a failed Attempt.
        comments = [_attempt_comment("a1"), _handoff_comment("h1"),
                    _attempt_comment("a2"), _handoff_comment("h2")]
        self.assertEqual(ralph_failure.count_attempts(comments), 2)

    def test_ignores_plain_comments(self):
        comments = [{"body": "just a note"}, _attempt_comment("a1")]
        self.assertEqual(ralph_failure.count_attempts(comments), 1)

    def test_accepts_plain_string_comments(self):
        comments = [ralph_failure.ATTEMPT_MARKER + " x", "unrelated"]
        self.assertEqual(ralph_failure.count_attempts(comments), 1)


class AttemptPlan(unittest.TestCase):
    def test_first_failure_records_but_does_not_block(self):
        plan = ralph_failure.attempt_plan(story(comments=[]), "gating red",
                                          max_attempts=3)
        self.assertTrue(plan.ok, plan.errors)
        self.assertFalse(plan.blocked)
        self.assertEqual(plan.attempt_no, 1)
        tokens = _flat(plan.commands)
        # exactly one terse comment, no relabel to blocked
        self.assertEqual(sum(1 for c in plan.commands if c[:3] == ["gh", "issue", "comment"]), 1)
        self.assertNotIn("state:blocked", tokens)

    def test_comment_carries_attempt_marker_and_reason(self):
        plan = ralph_failure.attempt_plan(story(comments=[]), "compile error", max_attempts=3)
        comment = next(c for c in plan.commands if c[:3] == ["gh", "issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertIn(ralph_failure.ATTEMPT_MARKER, body)
        self.assertIn("compile error", body)

    def test_reaching_limit_blocks_with_one_terse_comment(self):
        # two prior attempts already recorded; this third one reaches max=3.
        prior = [_attempt_comment("a1"), _attempt_comment("a2")]
        plan = ralph_failure.attempt_plan(story(number=9, state="in-progress", comments=prior),
                                          "still red", max_attempts=3)
        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.attempt_no, 3)
        # exactly one terse comment accompanies the block
        self.assertEqual(sum(1 for c in plan.commands if c[:3] == ["gh", "issue", "comment"]), 1)
        edit = next(c for c in plan.commands if c[:3] == ["gh", "issue", "edit"])
        self.assertIn("state:blocked", edit)

    def test_block_removes_the_current_state_label(self):
        prior = [_attempt_comment(), _attempt_comment()]
        plan = ralph_failure.attempt_plan(story(state="in-progress", comments=prior),
                                          "x", max_attempts=3)
        edit = next(c for c in plan.commands if c[:3] == ["gh", "issue", "edit"])
        self.assertIn("--add-label", edit)
        self.assertIn("state:blocked", edit)
        self.assertIn("--remove-label", edit)
        self.assertIn("state:in-progress", edit)

    def test_checkpoints_do_not_count_toward_the_limit(self):
        # two handoffs + one real attempt: still attempt #2 next, not blocked at max=3.
        comments = [_handoff_comment(), _attempt_comment("a1"), _handoff_comment()]
        plan = ralph_failure.attempt_plan(story(comments=comments), "red", max_attempts=3)
        self.assertEqual(plan.attempt_no, 2)
        self.assertFalse(plan.blocked)

    def test_custom_max_attempts_of_one_blocks_immediately(self):
        plan = ralph_failure.attempt_plan(story(comments=[]), "x", max_attempts=1)
        self.assertTrue(plan.blocked)
        self.assertEqual(plan.attempt_no, 1)

    def test_never_references_main(self):
        plan = ralph_failure.attempt_plan(story(comments=[]), "x", max_attempts=3)
        self.assertNotIn("main", _flat(plan.commands))


class CircuitBreakerPlan(unittest.TestCase):
    def _blocked(self, number):
        return story(number=number, state="blocked")

    def test_not_tripped_below_threshold(self):
        backlog = [self._blocked(10), story(number=11, state="ready")]
        plan = ralph_failure.circuit_breaker_plan(backlog, "octocat", circuit_breaker=2)
        self.assertTrue(plan.ok)
        self.assertFalse(plan.tripped)
        self.assertEqual(plan.commands, [])

    def test_trips_at_threshold_applies_needs_human_and_tags(self):
        backlog = [self._blocked(10), self._blocked(12),
                   story(number=11, state="ready")]
        plan = ralph_failure.circuit_breaker_plan(backlog, "octocat", circuit_breaker=2)
        self.assertTrue(plan.tripped)
        tokens = _flat(plan.commands)
        self.assertIn(ralph_failure.NEEDS_HUMAN_LABEL, tokens)
        # the configured handle is tagged in a comment
        comment = next(c for c in plan.commands if c[:3] == ["gh", "issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertIn("@octocat", body)

    def test_strips_leading_at_from_handle(self):
        backlog = [self._blocked(10), self._blocked(12)]
        plan = ralph_failure.circuit_breaker_plan(backlog, "@octocat", circuit_breaker=2)
        comment = next(c for c in plan.commands if c[:3] == ["gh", "issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertIn("@octocat", body)
        self.assertNotIn("@@", body)

    def test_targets_most_recently_blocked_story(self):
        backlog = [self._blocked(10), self._blocked(12), self._blocked(7)]
        plan = ralph_failure.circuit_breaker_plan(backlog, "octocat", circuit_breaker=2)
        self.assertEqual(plan.target, 12)
        for cmd in plan.commands:
            self.assertIn("12", cmd)

    def test_closed_blocked_stories_do_not_count(self):
        closed = self._blocked(10)
        closed["state"] = "CLOSED"
        backlog = [closed, self._blocked(12)]
        plan = ralph_failure.circuit_breaker_plan(backlog, "octocat", circuit_breaker=2)
        self.assertFalse(plan.tripped)


class CircuitBreakerHaltsSelection(unittest.TestCase):
    """AC#3: applying needs-human halts the loop -- tie the breaker output back to
    the selection engine (needs-human anywhere => HALT)."""

    def test_needs_human_label_makes_selection_halt(self):
        blocked = story(number=12, state="blocked")
        ready = story(number=11, state="ready")
        # before tripping, ready work is still selectable
        before = ralph_select.next_action([blocked, ready])
        self.assertEqual(before.kind, ralph_select.START)
        # apply the label the circuit breaker would add
        blocked["labels"].append({"name": ralph_failure.NEEDS_HUMAN_LABEL})
        after = ralph_select.next_action([blocked, ready])
        self.assertEqual(after.kind, ralph_select.HALT)


class KickbackReAttempt(unittest.TestCase):
    """AC#4: a story kicked back to state:ready (failed HIL bench test) is
    re-selected so it can be re-attempted on a fresh PR."""

    def test_hil_story_moved_back_to_ready_is_selected(self):
        kicked = story(number=7, type_="hil", state="ready")
        action = ralph_select.next_action([kicked])
        self.assertEqual(action.kind, ralph_select.START)
        self.assertEqual(action.number, 7)


class RunPlan(unittest.TestCase):
    def test_all_ok(self):
        res = ralph_failure.run_plan([["true"], ["true"]])
        self.assertTrue(res.ok)
        self.assertIsNone(res.failed)

    def test_fail_fast_records_failure(self):
        res = ralph_failure.run_plan([
            ["true"],
            ["sh", "-c", "echo BOOM; exit 3"],
            ["sh", "-c", "echo SHOULD_NOT_RUN"],
        ])
        self.assertFalse(res.ok)
        self.assertEqual(res.failed.returncode, 3)
        self.assertIn("BOOM", res.failed.output)
        self.assertEqual(len(res.steps), 2)


def _mockbin(tmp, gh_exit=0):
    log = os.path.join(tmp, "calls.log")
    path = os.path.join(tmp, "gh")
    with open(path, "w") as fh:
        fh.write('#!/usr/bin/env bash\n'
                 'echo "gh $*" >> "$RALPH_LOG"\n'
                 'exit %d\n' % gh_exit)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
    return log


class CliRecordAttempt(unittest.TestCase):
    def _run(self, story_obj, reason, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--record-attempt", "-", reason, config],
            cwd=REPO_ROOT, input=json.dumps(story_obj), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_records_attempt_via_mocked_gh(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(number=9, comments=[]), "gating red", config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("issue comment 9", calls)
            self.assertNotIn("state:blocked", calls)  # full.yml max_attempts=5

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            log = os.path.join(tmp, "calls.log")
            proc = self._run(story(comments=[]), "x", config, tmp, log)
            self.assertEqual(proc.returncode, 2)

    def test_gh_failure_exits_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp, gh_exit=1)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(comments=[]), "x", config, tmp, log)
            self.assertEqual(proc.returncode, 1)

    def test_missing_reason_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
            proc = subprocess.run(
                [RALPH, "--record-attempt", "-"],
                cwd=REPO_ROOT, input=json.dumps(story()), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            self.assertEqual(proc.returncode, 2)


class CliCheckBreaker(unittest.TestCase):
    def _backlog_file(self, tmp, stories):
        path = os.path.join(tmp, "backlog.json")
        with open(path, "w") as fh:
            json.dump(stories, fh)
        return path

    def _run(self, backlog_path, config, tmp, log):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], RALPH_LOG=log)
        return subprocess.run(
            [RALPH, "--check-breaker", backlog_path, config],
            cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def test_trips_and_tags_when_threshold_reached(self):
        # full.yml circuit_breaker=3, notify.github=octocat
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            backlog = self._backlog_file(tmp, [
                story(number=10, state="blocked"),
                story(number=12, state="blocked"),
                story(number=14, state="blocked"),
            ])
            proc = self._run(backlog, config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn(ralph_failure.NEEDS_HUMAN_LABEL, calls)
            self.assertIn("@octocat", calls)

    def test_not_tripped_runs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = _mockbin(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            backlog = self._backlog_file(tmp, [story(number=10, state="blocked")])
            proc = self._run(backlog, config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(log))  # nothing executed


class ResetOnBlockPlan(unittest.TestCase):
    """PRD #69: a blocked story's work is already quarantined by its branch.

    Every story works on its own branch, so a demotion has nothing to rewind
    and nothing to rescue. What is left is what a branch cannot do: push the
    work so a human can reach it, say what failed, and move the story to
    state:blocked.
    """

    def _plan(self, **kwargs):
        defaults = dict(
            story=feature_story(number=27, parent=18),
            reason="gating red",
            prd=prd_issue(number=18),
            branch_pattern="ralph/{issue}-{slug}",
        )
        defaults.update(kwargs)
        return ralph_failure.reset_on_block_plan(**defaults)

    def test_the_work_stays_on_the_storys_own_branch(self):
        plan = self._plan()
        self.assertTrue(plan.ok, plan.errors)
        push = next(c for c in plan.commands if c[0] == "git" and "push" in c)
        self.assertIn("HEAD:ralph/27-add-spi-driver", push)

    def test_the_feature_branch_is_neither_rewound_nor_force_pushed(self):
        flat = " ".join(_flat(self._plan().commands))
        self.assertNotIn("--force", flat)
        self.assertNotIn("feature/18", flat)

    def test_demoting_one_story_leaves_its_siblings_untouched(self):
        """Only this story's branch and this story's issue are named."""
        sibling = feature_story(number=28, parent=18)
        flat = " ".join(_flat(self._plan().commands))
        self.assertNotIn(str(sibling["number"]), flat)
        self.assertNotIn(ralph_iterate.branch_name(sibling), flat)

    def test_demotion_comment_describes_the_failure_and_names_the_branch(self):
        plan = self._plan()
        comment = next(c for c in plan.commands
                       if c[:3] == ["gh", "issue", "comment"])
        body = comment[comment.index("--body") + 1]
        self.assertIn("ralph/27-add-spi-driver", body)
        self.assertIn("gating red", body)

    def test_moves_story_to_blocked(self):
        plan = self._plan()
        edit = next(c for c in plan.commands
                    if c[:3] == ["gh", "issue", "edit"])
        self.assertIn("state:blocked", edit)
        self.assertIn("--remove-label", edit)
        self.assertIn("state:in-progress", edit)

    def test_orphan_story_returns_empty_plan(self):
        """An Orphan Story's demotion is unchanged -- no reset commands."""
        plan = ralph_failure.reset_on_block_plan(
            story=story(number=9, comments=[]), reason="gating red")
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.commands, [])

    def test_no_recorded_boundary_is_needed_to_demote(self):
        """The machinery that only served the rewind is gone, not inert."""
        self.assertFalse(hasattr(ralph_failure, "boundary_plan"))
        self.assertFalse(hasattr(ralph_failure, "BOUNDARY_MARKER"))
        plan = self._plan(story=feature_story(number=27, parent=18,
                                              comments=[]))
        self.assertTrue(plan.ok, plan.errors)

    def test_honors_a_custom_branch_pattern(self):
        plan = self._plan(branch_pattern="wip/{issue}-{slug}")
        self.assertIn("wip/27-add-spi-driver", " ".join(_flat(plan.commands)))

    def test_never_references_main(self):
        self.assertNotIn("main", _flat(self._plan().commands))

    def test_the_work_is_pushed_before_the_story_is_demoted(self):
        plan = self._plan()
        push = next(i for i, c in enumerate(plan.commands) if c[0] == "git")
        edit = next(i for i, c in enumerate(plan.commands)
                    if c[:3] == ["gh", "issue", "edit"])
        self.assertLess(push, edit)


class CliResetOnBlock(unittest.TestCase):
    def _run(self, story_obj, reason, config, tmp, log, prd_obj=None):
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"],
                   RALPH_LOG=log)
        args = [RALPH, "--reset-on-block", "-", reason, config]
        stdin = json.dumps(story_obj)
        if prd_obj is not None:
            prd_path = os.path.join(tmp, "prd.json")
            with open(prd_path, "w") as fh:
                json.dump(prd_obj, fh)
            args.append(prd_path)
        return subprocess.run(
            args, cwd=REPO_ROOT, input=stdin, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def _mockbin_git_gh(self, tmp, git_exit=0, gh_exit=0):
        log = os.path.join(tmp, "calls.log")
        for name, exit_code in [("git", git_exit), ("gh", gh_exit)]:
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write('#!/usr/bin/env bash\n'
                         'echo "%s $*" >> "$RALPH_LOG"\n'
                         'exit %d\n' % (name, exit_code))
            os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return log

    def test_resets_feature_story_via_mocked_git_gh(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin_git_gh(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            s = feature_story(number=27, parent=18)
            proc = self._run(s, "gating red", config, tmp, log,
                             prd_obj=prd_issue(number=18))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(log) as fh:
                calls = fh.read()
            self.assertIn("HEAD:ralph/27-add-spi-driver", calls)
            self.assertNotIn("--force", calls)
            self.assertIn("state:blocked", calls)

    def test_orphan_story_exits_zero_no_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin_git_gh(tmp)
            config = os.path.join(FIXTURES, "config", "valid", "full.yml")
            proc = self._run(story(number=9, comments=[]), "gating red",
                             config, tmp, log)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse(os.path.exists(log))

    def test_bad_config_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._mockbin_git_gh(tmp)
            config = os.path.join(FIXTURES, "config", "invalid", "missing-gating.yml")
            proc = self._run(feature_story(number=27, parent=18),
                             "x", config, tmp, log,
                             prd_obj=prd_issue(number=18))
            self.assertEqual(proc.returncode, 2)


class FailurePromptV1(unittest.TestCase):
    def setUp(self):
        self.assertTrue(os.path.isfile(PROMPT_V1), "prompts/failure.v1.md must be checked in")
        with open(PROMPT_V1) as fh:
            self.text = fh.read()

    def test_covers_the_failure_directives(self):
        low = self.text.lower()
        for needle in ["attempt", "state:blocked", "circuit breaker", "needs-human",
                       "checkpoint", "state:ready", "fresh pr", "terse"]:
            self.assertIn(needle, low, "failure.v1 prompt missing: %s" % needle)

    def test_uses_hil_terminology_not_hitl(self):
        self.assertNotIn("HITL", self.text)


if __name__ == "__main__":
    unittest.main()
