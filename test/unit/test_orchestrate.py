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

    def select_implementation(self, provider, alternate=None):
        """Commit a model catalog whose implementation role resolves to
        `provider`; the review role gets the other one, so the two roles keep
        their distinct model identities (#44). `alternate=False` commits the
        fixed-role option (#47)."""
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
            if alternate is not None:
                fh.write("  alternate: %s\n" % ("true" if alternate else "false"))

    def set_view_story(self, s):
        with open(os.path.join(self.queue, "story.json"), "w") as fh:
            json.dump(s, fh)

    def set_view_stories(self, *stories):
        """Answer `gh issue view N` per issue number, so a tick that works
        several stories in sequence sees the right record for each."""
        for s in stories:
            with open(os.path.join(self.queue, "story-%d.json" % s["number"]),
                      "w") as fh:
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
              f="$RALPH_GH_QUEUE_DIR/story-$3.json"
              [[ -f "$f" ]] || f="$RALPH_GH_QUEUE_DIR/story.json"
              cat "$f"
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
        # A tick that is itself running Ralph exports the provider binary
        # overrides (RALPH_CLAUDE / RALPH_CODEX). Inherited, they would win over
        # the fakes on PATH and this test would launch a *real* agent, so drop
        # them: the mocks are the only providers a test may run.
        for adapter in ralph_agent.PROVIDERS.values():
            e.pop(adapter.binary_env, None)
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


class TheTickRecordsTheModelAssignmentOnTheStory(unittest.TestCase):
    """AC (#46): starting a newly unassigned Story records the exact
    implementation and review model identities as durable Story labels, and an
    already-assigned Story is launched from its labels rather than from config."""

    def harness(self, provider="claude"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        h = TickHarness(tmp)
        h.select_implementation(provider)
        return h

    def _assigned(self, s, impl="claude-model", review="codex-model"):
        s = json.loads(json.dumps(s))
        s["labels"] = s["labels"] + [{"name": "model:impl:" + impl},
                                     {"name": "model:review:" + review}]
        return s

    def test_starting_a_story_creates_and_applies_both_assignment_labels(self):
        h = self.harness("claude")
        h.set_backlogs([story(7, "ready", "afk")], [])
        h.set_view_story(story(7, "ready", "afk"))
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("label create model:impl:claude-model" in ln for ln in log), log)
        self.assertTrue(any("label create model:review:codex-model" in ln for ln in log), log)
        applied = [ln for ln in log
                   if "issue edit 7" in ln and "model:impl:claude-model" in ln]
        self.assertTrue(applied, log)
        self.assertIn("model:review:codex-model", applied[0])

    def test_an_already_assigned_story_is_not_relabelled(self):
        # The tick's committed default is claude-model, but the story was
        # assigned codex-model: the assignment wins and is not rewritten.
        h = self.harness("claude")
        assigned = self._assigned(story(7, "ready", "afk"), impl="codex-model",
                                  review="claude-model")
        h.set_backlogs([assigned], [])
        h.set_view_story(assigned)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("label create model:" in ln for ln in log), log)
        self.assertFalse(any("--add-label model:" in ln for ln in log), log)

    def test_the_iteration_launches_the_assigned_model_not_the_default(self):
        h = self.harness("claude")
        assigned = self._assigned(story(7, "ready", "afk"), impl="codex-model",
                                  review="claude-model")
        h.set_backlogs([assigned], [])
        h.set_view_story(assigned)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        calls = h.agent_calls("codex")
        self.assertEqual(len(calls), 1, log)
        self.assertIn("--model codex-model", calls[0])
        self.assertEqual(h.agent_calls("claude"), [], log)

    def test_a_resumed_unassigned_story_is_assigned_before_its_iteration(self):
        # A story started before it had an assignment heals forward on resume
        # instead of staying blank.
        h = self.harness("claude")
        h.set_backlogs([story(5, "in-progress", "afk")], [])
        h.set_view_story(story(5, "in-progress", "afk"))
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("issue edit 5" in ln and "model:impl:claude-model" in ln
                            for ln in log), log)

    def test_a_repository_without_a_catalog_records_nothing_and_still_ticks(self):
        # The catalog stays optional (#44): no identities to persist, no labels.
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        h = TickHarness(tmp)  # full.yml declares no models:
        h.set_backlogs([story(7, "ready", "afk")], [])
        h.set_view_story(story(7, "ready", "afk"))
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("model:impl:" in ln for ln in log), log)
        self.assertTrue(h.agent_calls(), log)


class TheTickAlternatesTheRolesAcrossNewlyStartedStories(unittest.TestCase):
    """AC (#47): alternation across several consecutive newly started AFK and
    HIL Stories, driven through the real tick.

    The tick assigns each newly started Story before its iteration, so the
    observable behavior is the sequence of `model:impl:` labels it applies: the
    first Story runs the resolved order, and every following newly started Story
    -- AFK or HIL, it makes no difference -- swaps the pair.
    """

    def harness(self, provider="claude", alternate=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        h = TickHarness(tmp)
        h.select_implementation(provider, alternate=alternate)
        return h

    def _queue_in_sequence(self, h, stories):
        """Backlogs for a tick that starts each story in turn: the engine pops
        one backlog per --dry-run and one per --needs-freshness."""
        backlogs = []
        for i in range(len(stories)):
            remaining = stories[i:]
            backlogs.append(remaining)   # --dry-run -> start stories[i]
            backlogs.append(remaining)   # --needs-freshness
        backlogs.append([])              # --dry-run -> no-work, tick ends
        h.set_backlogs(*backlogs)
        h.set_view_stories(*stories)

    def _assigned_impl(self, h):
        """The implementation identity each `gh issue edit` recorded, in order."""
        out = []
        for line in h.log_lines():
            if "issue edit" not in line:
                continue
            for token in line.split():
                if token.startswith("model:impl:"):
                    out.append(token[len("model:impl:"):])
        return out

    def test_consecutive_afk_and_hil_stories_alternate_the_pair(self):
        h = self.harness("claude")
        stories = [story(71, "ready", "afk", prio=1), story(72, "ready", "hil", prio=2),
                   story(73, "ready", "afk", prio=3), story(74, "ready", "hil", prio=4)]
        self._queue_in_sequence(h, stories)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(len(h.agent_calls()), 4, h.log_lines())
        self.assertEqual(self._assigned_impl(h),
                         ["claude-model", "codex-model",
                          "claude-model", "codex-model"])

    def test_the_review_role_takes_the_other_half_of_each_pair(self):
        h = self.harness("claude")
        stories = [story(71, "ready", "afk", prio=1), story(72, "ready", "hil", prio=2)]
        self._queue_in_sequence(h, stories)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        first = [ln for ln in log if "issue edit 71" in ln and "model:impl:" in ln]
        second = [ln for ln in log if "issue edit 72" in ln and "model:impl:" in ln]
        self.assertTrue(first and second, log)
        self.assertIn("model:review:codex-model", first[0])
        self.assertIn("model:review:claude-model", second[0])

    def test_the_alternation_continues_in_the_next_tick(self):
        # AC: alternation state survives across ticks -- the second tick's first
        # newly started Story swaps, it does not restart at the resolved order.
        h = self.harness("claude")
        self._queue_in_sequence(h, [story(71, "ready", "afk")])
        self.assertEqual(h.run().returncode, 0)
        os.remove(os.path.join(h.queue, "counter"))
        self._queue_in_sequence(h, [story(72, "ready", "hil")])
        self.assertEqual(h.run().returncode, 0)
        self.assertEqual(self._assigned_impl(h), ["claude-model", "codex-model"])

    def test_a_resumed_story_between_two_new_ones_keeps_its_roles(self):
        # The middle story is already assigned: it neither gets relabelled nor
        # consumes the swap the next newly started Story is owed.
        h = self.harness("claude")
        resumed = story(72, "in-progress", "afk", prio=2)
        resumed["labels"] += [{"name": "model:impl:claude-model"},
                              {"name": "model:review:codex-model"}]
        stories = [resumed, story(73, "ready", "afk", prio=3)]
        self._queue_in_sequence(h, stories)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self._assigned_impl(h), ["claude-model"])
        self.assertFalse(any("issue edit 72" in ln for ln in h.log_lines()),
                         h.log_lines())

    def test_the_fixed_role_option_holds_every_story_at_the_resolved_order(self):
        h = self.harness("claude", alternate=False)
        stories = [story(71, "ready", "afk", prio=1), story(72, "ready", "hil", prio=2),
                   story(73, "ready", "afk", prio=3)]
        self._queue_in_sequence(h, stories)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self._assigned_impl(h),
                         ["claude-model", "claude-model", "claude-model"])


if __name__ == "__main__":
    unittest.main()
