"""Orchestration tests for the Ralph tick loop (US-011, ADR-0002/0004).

`bin/ralph.sh` is the unattended **tick**: it guards with `flock` (one tick per
superproject), resumes an in-progress story before scanning for new
`state:ready` work, works multiple eligible stories in sequence until no
eligible work remains, and -- when the `claude` CLI signals session-limit
exhaustion -- checkpoints the current story via a Handoff and ends cleanly.

The bats suite (`test/bats/orchestration.bats`) drives the same script against
mocked `claude`/`gh` on PATH; bats is not installed in this environment, so
these stdlib-`unittest` subprocess tests are the executed green gate (the same
"mock the CLIs on PATH via $RALPH_LOG" pattern the completion stages use).
"""
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")
FULL_CONFIG = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid", "full.yml")

SESSION_LIMIT_EXIT = "91"


def story(number, state, type_="afk", prio=1, needs_human=False):
    labels = [{"name": "type:" + type_}, {"name": "prio:%d" % prio},
              {"name": "state:" + state}]
    if needs_human:
        labels.append({"name": "needs-human"})
    body = "## Acceptance Criteria\n- [ ] does the thing\n\nDepends on: None\n"
    if type_ == "hil":
        body += "\n## Bench Test Procedure\n- poke it\n"
    return {"number": number, "title": "Story %d" % number, "labels": labels,
            "body": body, "state": "OPEN"}


def _write_exec(path, contents):
    with open(path, "w") as fh:
        fh.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TickHarness:
    """A throwaway superproject: .ralph.yml, a .git/ lock dir, mock claude/gh/git
    on PATH, and a queue of backlog responses the mock `gh issue list` pops."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.log = os.path.join(tmp, "ralph.log")
        self.queue = os.path.join(tmp, "ghq")
        os.makedirs(self.queue)
        os.makedirs(os.path.join(tmp, ".git"))
        os.makedirs(os.path.join(tmp, "mockbin"))
        with open(FULL_CONFIG) as fh:
            with open(os.path.join(tmp, ".ralph.yml"), "w") as out:
                out.write(fh.read())
        self._write_mocks()

    def set_backlogs(self, *backlogs):
        for i, backlog in enumerate(backlogs):
            with open(os.path.join(self.queue, "%d.json" % i), "w") as fh:
                json.dump(backlog, fh)

    def set_view_story(self, s):
        with open(os.path.join(self.queue, "story.json"), "w") as fh:
            json.dump(s, fh)

    def _write_mocks(self):
        mb = os.path.join(self.tmp, "mockbin")
        _write_exec(os.path.join(mb, "gh"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "gh $*" >> "$RALPH_LOG"
            if [[ "$1 $2" == "issue list" ]]; then
              n=$(cat "$RALPH_GH_QUEUE_DIR/counter" 2>/dev/null || echo 0)
              echo $((n + 1)) > "$RALPH_GH_QUEUE_DIR/counter"
              f="$RALPH_GH_QUEUE_DIR/$n.json"
              if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
            elif [[ "$1 $2" == "issue view" ]]; then
              cat "$RALPH_GH_QUEUE_DIR/story.json"
            fi
            """))
        _write_exec(os.path.join(mb, "claude"), textwrap.dedent("""\
            #!/usr/bin/env bash
            cat > /dev/null
            echo "claude action=${RALPH_ITERATION_ACTION:-} issue=${RALPH_ITERATION_ISSUE:-}" >> "$RALPH_LOG"
            exit "${RALPH_CLAUDE_EXIT:-0}"
            """))
        _write_exec(os.path.join(mb, "git"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "git $*" >> "$RALPH_LOG"
            exit 0
            """))

    def env(self, claude_exit="0"):
        e = dict(os.environ)
        e["PATH"] = os.path.join(self.tmp, "mockbin") + os.pathsep + e["PATH"]
        e["RALPH_LOG"] = self.log
        e["RALPH_GH_QUEUE_DIR"] = self.queue
        e["RALPH_SESSION_LIMIT_EXIT"] = SESSION_LIMIT_EXIT
        e["RALPH_CLAUDE_EXIT"] = claude_exit
        return e

    def run(self, claude_exit="0"):
        return subprocess.run([RALPH_SH], cwd=self.tmp, env=self.env(claude_exit),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [ln.rstrip("\n") for ln in fh if ln.strip()]


class OrchestrationTest(unittest.TestCase):
    def harness(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return TickHarness(tmp)

    def test_overlapping_tick_exits_immediately(self):
        # AC: only one tick per superproject; an overlapping tick exits at once.
        h = self.harness()
        h.set_backlogs([story(7, "ready")])
        lock_path = os.path.join(h.tmp, ".git", "ralph-tick.lock")
        held = open(lock_path, "w")
        self.addCleanup(held.close)
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("already running", proc.stdout.lower())
        # It did no work: no claude iteration was launched.
        self.assertFalse(any("claude" in ln for ln in h.log_lines()), h.log_lines())

    def test_resume_first_before_ready(self):
        # AC: resume an in-progress story before scanning for new ready work.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress"), story(7, "ready")], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        claude_calls = [ln for ln in h.log_lines() if ln.startswith("claude ")]
        self.assertEqual(len(claude_calls), 1, h.log_lines())
        self.assertIn("issue=5", claude_calls[0])
        self.assertIn("action=resume", claude_calls[0])

    def test_works_multiple_stories_in_sequence(self):
        # AC: a tick works multiple eligible stories in sequence until none remain.
        h = self.harness()
        h.set_backlogs(
            [story(7, "ready"), story(8, "ready", prio=2)],  # -> start #7
            [story(8, "ready", prio=2)],                     # #7 done -> start #8
            [],                                              # -> no-work, stop
        )
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        claude_calls = [ln for ln in h.log_lines() if ln.startswith("claude ")]
        self.assertEqual(len(claude_calls), 2, h.log_lines())
        self.assertIn("issue=7", claude_calls[0])
        self.assertIn("issue=8", claude_calls[1])

    def test_session_limit_checkpoints_and_ends(self):
        # AC: session-limit exhaustion from the claude CLI checkpoints via Handoff
        # and the tick ends cleanly.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress")])
        h.set_view_story(story(5, "in-progress"))
        proc = h.run(claude_exit=SESSION_LIMIT_EXIT)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        claude_calls = [ln for ln in log if ln.startswith("claude ")]
        self.assertEqual(len(claude_calls), 1, log)          # did not continue
        # It fetched the story and wrote a Handoff (issue comment) for #5.
        self.assertTrue(any("issue view 5" in ln for ln in log), log)
        self.assertTrue(any("issue comment 5" in ln for ln in log), log)
        self.assertIn("session limit", proc.stdout.lower())

    def test_halt_on_needs_human(self):
        # AC: the loop halts (needs-human) without launching an iteration.
        h = self.harness()
        h.set_backlogs([story(9, "ready", needs_human=True)])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(any("claude" in ln for ln in h.log_lines()), h.log_lines())
        self.assertIn("halt", proc.stdout.lower())

    def test_no_work_empty_backlog_ends_cleanly(self):
        h = self.harness()
        h.set_backlogs([])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(any("claude" in ln for ln in h.log_lines()), h.log_lines())


if __name__ == "__main__":
    unittest.main()
