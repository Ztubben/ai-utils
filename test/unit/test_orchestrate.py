"""Orchestration tests for the Ralph tick loop (US-011, ADR-0002/0004).

`bin/ralph.sh` is the unattended **tick**: it guards with `flock` (one tick per
superproject), resumes an in-progress story before scanning for new
`state:ready` work, works multiple eligible stories in sequence until no
eligible work remains, and -- when the launched agent signals session-limit
exhaustion -- checkpoints the current story via a Handoff and ends cleanly.

The tick names no provider (#45): it launches the Implementation Agent through
the adapter interface, so the same script drives whichever provider the target
repository's model catalog selects. The harness therefore puts a fake binary for
*every* provider on PATH and asserts which one the tick actually ran.

The bats suite (`test/bats/orchestration.bats`) drives the same script against
mocked provider CLIs/`gh` on PATH; bats is not installed in this environment, so
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
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import ralph_agent  # noqa: E402

PROVIDERS = sorted(ralph_agent.PROVIDERS)
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")
FULL_CONFIG = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid", "full.yml")

SESSION_LIMIT_EXIT = "91"
STORY_COMPLETE_MARKER = "RALPH-STORY-COMPLETE"


def story(number, state, type_="afk", prio=1, needs_human=False):
    labels = [{"name": "type:" + type_}, {"name": "prio:%d" % prio},
              {"name": "state:" + state}]
    if needs_human:
        labels.append({"name": "needs-human"})
    body = "## Acceptance Criteria\n- [ ] does the thing\n\nParent: None\nDepends on: None\n"
    if type_ == "hil":
        body += "\n## Bench Test Procedure\n- poke it\n"
    return {"number": number, "title": "Story %d" % number, "labels": labels,
            "body": body, "state": "OPEN"}


def _write_exec(path, contents):
    with open(path, "w") as fh:
        fh.write(contents)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class TickHarness:
    """A throwaway superproject: .ralph.yml, a .git/ lock dir, a fake binary for
    every provider plus mock gh/git on PATH, and a queue of backlog responses the
    mock `gh issue list` pops."""

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

    def select_implementation(self, provider):
        """Commit a model catalog whose implementation role resolves to
        `provider`; the review role gets the other one, so the two roles keep
        their distinct model identities (#44)."""
        other = "codex" if provider == "claude" else "claude"
        with open(os.path.join(self.tmp, ".ralph.yml"), "a") as fh:
            fh.write(textwrap.dedent("""\
                models:
                  profiles:
                    - key: impl
                      provider: %s
                      model: %s-model
                    - key: rev
                      provider: %s
                      model: %s-model
                  defaults:
                    implementation: impl
                    review: rev
                """ % (provider, provider, other, other)))

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
        for provider in PROVIDERS:
            # One fake per provider, logging its own argv: the tick is only
            # provider-neutral if the assertion can tell which one it ran.
            _write_exec(os.path.join(mb, provider), textwrap.dedent("""\
                #!/usr/bin/env bash
                cat > /dev/null
                echo "%s $* action=${RALPH_ITERATION_ACTION:-} issue=${RALPH_ITERATION_ISSUE:-}" >> "$RALPH_LOG"
                [[ -n "${RALPH_AGENT_EMIT:-}" ]] && printf '%%s\\n' "$RALPH_AGENT_EMIT"
                exit "${RALPH_AGENT_EXIT:-0}"
                """ % provider))
        _write_exec(os.path.join(mb, "git"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "git $*" >> "$RALPH_LOG"
            exit 0
            """))

    def env(self, agent_exit="0", agent_emit=""):
        e = dict(os.environ)
        e["PATH"] = os.path.join(self.tmp, "mockbin") + os.pathsep + e["PATH"]
        e["RALPH_LOG"] = self.log
        e["RALPH_GH_QUEUE_DIR"] = self.queue
        e["RALPH_SESSION_LIMIT_EXIT"] = SESSION_LIMIT_EXIT
        e["RALPH_AGENT_EXIT"] = agent_exit
        e["RALPH_AGENT_EMIT"] = agent_emit
        return e

    def run(self, agent_exit="0", agent_emit=""):
        return subprocess.run([RALPH_SH], cwd=self.tmp,
                              env=self.env(agent_exit, agent_emit),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def agent_calls(self, provider=None):
        """Log lines written by a fake provider binary (all, or just one)."""
        wanted = [provider] if provider else list(PROVIDERS)
        return [ln for ln in self.log_lines()
                if any(ln.startswith(p + " ") for p in wanted)]

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
        # It did no work: no agent iteration was launched.
        self.assertEqual(h.agent_calls(), [], h.log_lines())

    def test_resume_first_before_ready(self):
        # AC: resume an in-progress story before scanning for new ready work.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress"), story(7, "ready")], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        agent_calls = h.agent_calls()
        self.assertEqual(len(agent_calls), 1, h.log_lines())
        self.assertIn("issue=5", agent_calls[0])
        self.assertIn("action=resume", agent_calls[0])

    def test_works_multiple_stories_in_sequence(self):
        # AC: a tick works multiple eligible stories in sequence until none remain.
        # Queue slots: dry-run -> freshness -> dry-run -> freshness -> dry-run(no-work) -> ready-features
        bl1 = [story(7, "ready"), story(8, "ready", prio=2)]
        bl2 = [story(8, "ready", prio=2)]
        h = self.harness()
        h.set_backlogs(
            bl1,   # dry-run -> start #7
            bl1,   # --needs-freshness #7
            bl2,   # dry-run -> start #8
            bl2,   # --needs-freshness #8
            [],    # dry-run -> no-work, stop
        )
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        agent_calls = h.agent_calls()
        self.assertEqual(len(agent_calls), 2, h.log_lines())
        self.assertIn("issue=7", agent_calls[0])
        self.assertIn("issue=8", agent_calls[1])

    def test_session_limit_checkpoints_and_ends(self):
        # AC: session-limit exhaustion from the launched agent checkpoints via Handoff
        # and the tick ends cleanly.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress")])
        h.set_view_story(story(5, "in-progress"))
        proc = h.run(agent_exit=SESSION_LIMIT_EXIT)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        agent_calls = h.agent_calls()
        self.assertEqual(len(agent_calls), 1, log)          # did not continue
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
        self.assertEqual(h.agent_calls(), [], h.log_lines())
        self.assertIn("halt", proc.stdout.lower())

    def test_no_work_empty_backlog_ends_cleanly(self):
        h = self.harness()
        h.set_backlogs([])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(h.agent_calls(), [], h.log_lines())

    def test_start_moves_ready_story_to_in_progress(self):
        # AC: a `start` action transitions the story state:ready -> state:in-progress
        # before its first iteration, so checkpoint/partial/completion see the
        # expected state (and the story resumes rather than re-starts next pass).
        h = self.harness()
        h.set_backlogs([story(7, "ready", "afk")], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(
            any("issue edit 7" in ln and "state:in-progress" in ln
                and "state:ready" in ln for ln in log), log)

    def test_resume_does_not_relabel_the_story(self):
        # AC: `resume` is already state:in-progress; the tick must not re-label it.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress", "afk")], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertFalse(any("issue edit" in ln for ln in h.log_lines()),
                         h.log_lines())

    def test_green_afk_story_is_auto_merged_and_closed(self):
        # AC: an iteration that emits the done-signal on a type:afk story is
        # promoted via --complete-afk (auto-merge into base + close), not
        # re-selected forever (the bug: green story never leaves the backlog).
        h = self.harness()
        h.set_backlogs([story(7, "ready", "afk")], [])  # then no-work -> stop
        h.set_view_story(story(7, "ready", "afk"))
        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        agent_calls = h.agent_calls()
        self.assertEqual(len(agent_calls), 1, log)  # promoted, not re-run
        self.assertTrue(any("pr merge" in ln for ln in log), log)
        self.assertTrue(any("issue close 7" in ln for ln in log), log)

    def test_green_hil_story_opens_pr_to_awaiting_bench(self):
        # AC: a green type:hil story is promoted via --complete-hil (open PR +
        # move to state:awaiting-bench); it is never merged or closed.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress", "hil")], [])
        h.set_view_story(story(5, "in-progress", "hil"))
        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("pr create" in ln for ln in log), log)
        self.assertTrue(any("state:awaiting-bench" in ln for ln in log), log)
        self.assertFalse(any("pr merge" in ln for ln in log), log)
        self.assertFalse(any("issue close" in ln for ln in log), log)

    def test_partial_iteration_is_not_promoted(self):
        # AC: an iteration WITHOUT the done-signal made only partial progress and
        # must not be promoted (no completion CLI runs); the story is left for a
        # later pass. Here the backlog empties out so the tick then stops.
        h = self.harness()
        h.set_backlogs([story(7, "ready", "afk")], [])
        h.set_view_story(story(7, "ready", "afk"))
        proc = h.run()  # no marker emitted
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(h.agent_calls(), log)
        self.assertFalse(any("pr merge" in ln or "pr create" in ln for ln in log), log)


class TheTickDrivesWhicheverAdapterTheCatalogSelects(unittest.TestCase):
    """AC (#45): a tick implements a Story with the Codex adapter selected, and
    with the Claude adapter selected, driven against fake provider binaries on
    PATH -- and the orchestration behaves identically either way."""

    def harness(self, provider):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        h = TickHarness(tmp)
        h.select_implementation(provider)
        return h

    def other(self, provider):
        return "codex" if provider == "claude" else "claude"

    def test_a_tick_implements_a_story_with_the_selected_adapter(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                h = self.harness(provider)
                h.set_backlogs([story(7, "ready", "afk")], [])
                h.set_view_story(story(7, "ready", "afk"))
                proc = h.run()
                self.assertEqual(proc.returncode, 0, proc.stdout)
                log = h.log_lines()
                calls = h.agent_calls(provider)
                self.assertEqual(len(calls), 1, log)
                self.assertIn("issue=7", calls[0])
                self.assertIn("action=start", calls[0])
                # ... running the exact model identity the catalog configured ...
                self.assertIn("--model %s-model" % provider, calls[0])
                # ... and only that provider: the review model is never launched
                # as the Implementation Agent.
                self.assertEqual(h.agent_calls(self.other(provider)), [], log)

    def test_a_done_signal_still_promotes_whichever_adapter_ran(self):
        # AC: existing orchestration behavior is unchanged -- a done-signal
        # promotes the green AFK story (auto-merge + close) via either adapter.
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                h = self.harness(provider)
                h.set_backlogs([story(7, "ready", "afk")], [])
                h.set_view_story(story(7, "ready", "afk"))
                proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                log = h.log_lines()
                self.assertEqual(len(h.agent_calls(provider)), 1, log)  # not re-run
                self.assertTrue(any("pr merge" in ln for ln in log), log)
                self.assertTrue(any("issue close 7" in ln for ln in log), log)

    def test_session_exhaustion_still_checkpoints_whichever_adapter_ran(self):
        # AC: existing orchestration behavior is unchanged -- exhaustion
        # checkpoints via Handoff and ends the tick, via either adapter.
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                h = self.harness(provider)
                h.set_backlogs([story(5, "in-progress")])
                h.set_view_story(story(5, "in-progress"))
                proc = h.run(agent_exit=SESSION_LIMIT_EXIT)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                log = h.log_lines()
                self.assertEqual(len(h.agent_calls()), 1, log)   # did not continue
                self.assertTrue(any("issue comment 5" in ln for ln in log), log)
                self.assertIn("session limit", proc.stdout.lower())

    def test_an_infrastructure_failure_is_not_promoted_and_not_a_checkpoint(self):
        # AC: the three outcomes are distinct all the way out to the tick. A
        # crashing provider is neither green nor exhausted: the story stays
        # in-progress for a later pass, exactly as a partial pass does.
        h = self.harness("codex")
        h.set_backlogs([story(7, "ready", "afk")], [])
        h.set_view_story(story(7, "ready", "afk"))
        proc = h.run(agent_exit="1")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertEqual(len(h.agent_calls("codex")), 1, log)
        self.assertFalse(any("pr merge" in ln or "pr create" in ln for ln in log), log)
        self.assertFalse(any("issue comment" in ln for ln in log), log)


if __name__ == "__main__":
    unittest.main()
