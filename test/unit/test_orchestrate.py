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
import ralph_failure  # noqa: E402
import ralph_handoff  # noqa: E402

PROVIDERS = sorted(ralph_agent.PROVIDERS)
RALPH_SH = os.path.join(REPO_ROOT, "bin", "ralph.sh")
FULL_CONFIG = os.path.join(REPO_ROOT, "test", "fixtures", "config", "valid", "full.yml")

SESSION_LIMIT_EXIT = "91"
STORY_COMPLETE_MARKER = "RALPH-STORY-COMPLETE"
# The line the claude CLI actually emits at its session limit (#65). It carries
# neither exit 91 nor the legacy "usage limit reached" marker.
CURRENT_SESSION_LIMIT_OUTPUT = ("You've hit your session limit \u00b7 resets 9pm (Europe/Stockholm)")


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


def feature_story(number, state, type_="afk", parent=42, prio=1):
    """A Story belonging to the Feature whose PRD is *parent*."""
    s = story(number, state, type_, prio=prio)
    s["body"] = s["body"].replace("Parent: None", "Parent: #%d" % parent)
    return s


def prd_issue(number=42, title="Per-Story pull requests", state="OPEN"):
    return {"number": number, "title": title, "state": state,
            "labels": [{"name": "prd"}, {"name": "state:ready"}],
            "body": "## What to build\nthe Feature\n\nDepends on: None\n"}


FEATURE_BRANCH = "feature/42-per-story-pull-requests"


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
        self.ralph_cli = None
        self.log = os.path.join(tmp, "ralph.log")
        self.queue = os.path.join(tmp, "ghq")
        os.makedirs(self.queue)
        os.makedirs(os.path.join(tmp, ".git"))
        os.makedirs(os.path.join(tmp, "mockbin"))
        with open(FULL_CONFIG) as fh:
            with open(os.path.join(tmp, ".ralph.yml"), "w") as out:
                out.write(fh.read())
        self._write_mocks()

    def break_subcommand(self, sub, hide_from_help=False):
        """Point `$RALPH_CLI` at a `ralph` that has lost one subcommand.

        Everything else delegates to the real `bin/ralph`, so this reproduces the
        actual 2026-08-28 failure -- a checkout whose `ralph` predates a
        subcommand `bin/ralph.sh` calls -- rather than a wholly fake CLI.
        `hide_from_help` also drops it from the usage text, which is what the
        tick's preflight reads; leaving it listed simulates the subtler case
        where the subcommand is advertised but fails at the point of use.
        """
        # `read_base_branch` resolves lib/ as `dirname($RALPH_BIN)/../lib`, so the
        # wrapper has to sit in a bin/ with a lib/ beside it.
        home = os.path.join(self.tmp, "fakehome")
        os.makedirs(os.path.join(home, "bin"), exist_ok=True)
        for shared in ("lib", "schema", "prompts"):
            link = os.path.join(home, shared)
            if not os.path.exists(link):
                os.symlink(os.path.join(REPO_ROOT, shared), link)
        path = os.path.join(home, "bin", "ralph")
        _write_exec(path, textwrap.dedent("""\
            #!/usr/bin/env bash
            if [[ "$1" == "%(sub)s" ]]; then
              echo "ralph: unknown command: %(sub)s" >&2
              exit 2
            fi
            out="$("%(real)s" "$@")"; rc=$?
            if %(hide)s && [[ "$1" == "--help" || "$1" == "-h" || -z "${1:-}" ]]; then
              printf '%%s\\n' "$out" | grep -vF -- "%(sub)s"
            else
              printf '%%s\\n' "$out"
            fi
            exit "$rc"
            """ % {"sub": sub,
                   "real": os.path.join(REPO_ROOT, "bin", "ralph"),
                   "hide": "true" if hide_from_help else "false"}))
        self.ralph_cli = path
        return path

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

    def set_review_window(self, minutes, poll_seconds, max_rounds=None):
        """Commit a review window short enough for a test to sit through."""
        with open(os.path.join(self.tmp, ".ralph.yml"), "a") as fh:
            fh.write("review:\n  wait_minutes: %s\n  poll_seconds: %s\n"
                     % (minutes, poll_seconds))
            if max_rounds is not None:
                fh.write("  max_rounds: %d\n" % max_rounds)

    def set_pull_requests(self, listing, view=None):
        """Answer `gh pr list` and `gh pr view`, so a story In Review has the
        marked pull request the review path works against."""
        with open(os.path.join(self.queue, "prs.json"), "w") as fh:
            json.dump(listing, fh)
        with open(os.path.join(self.queue, "pr-view.json"), "w") as fh:
            json.dump(view if view is not None else (listing or [{}])[0], fh)

    def mock_make(self):
        """full.yml's gating steps shell `make`; the completion pass runs them."""
        _write_exec(os.path.join(self.tmp, "mockbin", "make"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "make $*" >> "$RALPH_LOG"
            """))

    def set_remote_branches(self, *names):
        """The branches `git ls-remote` reports origin already carries."""
        with open(os.path.join(self.queue, "branches.txt"), "w") as fh:
            fh.write("".join(name + "\n" for name in names))

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
            if [[ -n "${RALPH_LOCK_PROBE:-}" ]]; then
              # Probe from inside the tick: can anyone else take the tick lock
              # right now? Answering "no" is what "the tick holds its lock while
              # it waits" means in practice.
              if flock -n "$RALPH_LOCK_PROBE" -c true 2>/dev/null; then
                echo "tick-lock free" >> "$RALPH_LOG"
              else
                echo "tick-lock held" >> "$RALPH_LOG"
              fi
            fi
            if [[ "$1 $2" == "issue list" ]]; then
              n=$(cat "$RALPH_GH_QUEUE_DIR/counter" 2>/dev/null || echo 0)
              echo $((n + 1)) > "$RALPH_GH_QUEUE_DIR/counter"
              f="$RALPH_GH_QUEUE_DIR/$n.json"
              if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
            elif [[ "$1 $2" == "issue view" ]]; then
              f="$RALPH_GH_QUEUE_DIR/story-$3.json"
              [[ -f "$f" ]] || f="$RALPH_GH_QUEUE_DIR/story.json"
              cat "$f"
            elif [[ "$1 $2" == "pr list" ]]; then
              f="$RALPH_GH_QUEUE_DIR/prs.json"
              if [[ -f "$f" ]]; then cat "$f"; else echo "[]"; fi
            elif [[ "$1 $2" == "pr view" ]]; then
              cat "$RALPH_GH_QUEUE_DIR/pr-view.json"
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
        # `git` answers only what a tick actually asks it, and only when the
        # test has pinned an answer: a resolvable head is what lets the review
        # bundle be assembled against this fake checkout.
        _write_exec(os.path.join(mb, "git"), textwrap.dedent("""\
            #!/usr/bin/env bash
            echo "git $*" >> "$RALPH_LOG"
            if [[ "$1" == "ls-remote" ]]; then
              # Which branches origin already has. Unpinned means "none", which
              # is what an unstarted Feature looks like.
              b="${@: -1}"
              grep -qxF "$b" "$RALPH_GH_QUEUE_DIR/branches.txt" 2>/dev/null || exit 2
              exit 0
            fi
            if [[ "$1" == "rev-parse" ]]; then
              case "$2" in
                --*) ;;
                HEAD) cat "$RALPH_GH_QUEUE_DIR/head.txt" 2>/dev/null || true ;;
                *) [[ -f "$RALPH_GH_QUEUE_DIR/head.txt" ]] && echo "$2" ;;
              esac
            fi
            if [[ "$1 $2" == "diff --name-only" ]]; then
              cat "$RALPH_GH_QUEUE_DIR/changed.txt" 2>/dev/null || true
            fi
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
        # The same tick also leaks its own in-flight story and knobs into the
        # environment; inherited, they would steer the tick under test.
        for leaked in ("RALPH_ITERATION_ACTION", "RALPH_ITERATION_ISSUE",
                       "RALPH_SESSION_LIMIT_MARKER", "RALPH_CONFIG",
                       "RALPH_MAX_ITERATIONS", "RALPH_CLI"):
            e.pop(leaked, None)
        e["RALPH_AGENT_EXIT"] = agent_exit
        e["RALPH_AGENT_EMIT"] = agent_emit
        if self.ralph_cli:
            e["RALPH_CLI"] = self.ralph_cli
        return e

    def run(self, agent_exit="0", agent_emit="", lock_probe=False):
        env = self.env(agent_exit, agent_emit)
        if lock_probe:
            env["RALPH_LOCK_PROBE"] = os.path.join(self.tmp, ".git",
                                                   "ralph-tick.lock")
        return subprocess.run([RALPH_SH], cwd=self.tmp, env=env,
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

    def test_session_limit_wording_ends_the_tick_without_retrying(self):
        # AC (#65): a session-limit hit on the *first* iteration of a tick ends
        # the tick via checkpoint_story/RC_SESSION_LIMIT with no retry of the
        # same story -- even when the CLI signals it only in its output, with an
        # ordinary failure exit code rather than the legacy 91.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress")])
        h.set_view_story(story(5, "in-progress"))
        proc = h.run(agent_exit="1", agent_emit=CURRENT_SESSION_LIMIT_OUTPUT)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        claude_calls = [ln for ln in log if ln.startswith("claude ")]
        self.assertEqual(len(claude_calls), 1, log)          # no retry-storm
        self.assertTrue(any("issue comment 5" in ln for ln in log), log)
        self.assertIn("session limit", proc.stdout.lower())

    def test_legacy_session_limit_marker_still_ends_the_tick(self):
        # AC (#65): no regression on the old "usage limit reached" wording.
        h = self.harness()
        h.set_backlogs([story(5, "in-progress")])
        h.set_view_story(story(5, "in-progress"))
        proc = h.run(agent_exit="0", agent_emit="Claude usage limit reached.")
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertEqual(len([ln for ln in log if ln.startswith("claude ")]), 1, log)
        self.assertTrue(any("issue comment 5" in ln for ln in log), log)

    def test_a_ralph_missing_launch_agent_refuses_to_tick(self):
        # A tick checks out branches, so `$RALPH_BIN` can end up older than this
        # script. Caught at tick start, before the backlog is touched: on
        # 2026-08-28 the tick got as far as labelling #48 state:in-progress and
        # then failed to launch 24 times, leaving the story wrongly in-progress.
        h = self.harness()
        h.break_subcommand("--launch-agent", hide_from_help=True)
        h.set_backlogs([story(7, "ready")], [])
        proc = h.run()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("--launch-agent", proc.stdout)
        self.assertIn("refusing to tick", proc.stdout)
        log = h.log_lines()
        self.assertEqual(h.agent_calls(), [], log)
        # crucially: no story was moved to in-progress on the way out
        self.assertFalse([ln for ln in log if "issue edit" in ln], log)

    def test_a_failed_launch_ends_the_tick_instead_of_spinning(self):
        # The same story is selected first on every pass (resume-first), so a
        # launch that cannot run re-runs identically until RALPH_MAX_ITERATIONS.
        # It must end the tick, and non-zero, so the scheduler can see it: the
        # 2026-08-28 tick spun 24 times launching nothing and still exited 0.
        h = self.harness()
        h.break_subcommand("--launch-agent")  # still advertised, fails in use
        h.set_backlogs([story(5, "in-progress")], [story(5, "in-progress")], [])
        h.set_view_story(story(5, "in-progress"))
        proc = h.run()
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("ending tick", proc.stdout)
        self.assertEqual(h.agent_calls(), [], h.log_lines())
        # one selection, not a storm: the story is never re-selected this tick
        resumes = [ln for ln in proc.stdout.splitlines() if ln.startswith("ralph: resume #5")]
        self.assertEqual(len(resumes), 1, proc.stdout)

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

    def test_in_review_story_does_not_relaunch_the_implementation_model(self):
        h = self.harness()
        h.set_backlogs([story(7, "in-review", "afk")])
        h.set_view_story(story(7, "in-review", "afk"))
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(h.agent_calls(), [], h.log_lines())
        self.assertIn("stays in review", proc.stdout)
        # With no marked pull request there is nothing to wait for, so the tick
        # ends the wait at once rather than sitting out the window.
        self.assertIn("nothing to negotiate", proc.stdout)

    def test_in_review_story_with_a_marked_pr_runs_one_review_round(self):
        # AC (#53): a marked pull request In Review triggers a review round, and
        # the tick then parks rather than doing more implementation work on that
        # story. (The per-head guard that makes it exactly one round per head is
        # the round tool's, covered in test_review_round.py.)
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk")])
        h.set_view_story(story(7, "in-review", "afk"))
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #7\n"
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
             "reviews": [], "comments": []})
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any(ln.startswith("gh pr view 70") for ln in log), log)
        self.assertEqual(h.agent_calls(), [], log)
        self.assertIn("stays in review", proc.stdout)

    def reviewed_pull_request(self, h, issue=7, head="a" * 40):
        """A marked pull request whose head already carries its Ralph review."""
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #%d\n" % issue
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": head, "baseRefOid": "b" * 40, "comments": [],
             "reviews": [{"body": "<!-- ralph-review:v1 head=%s -->" % head}]})

    def test_waiting_for_review_spends_no_invocation_and_starts_no_other_story(self):
        # AC (#54): the tick waits inside its own process; waiting costs no
        # model invocation, and no other Story is picked up while it waits.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk"), story(8, "ready", "afk")])
        h.set_view_story(story(7, "in-review", "afk"))
        self.reviewed_pull_request(h)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertEqual(h.agent_calls(), [], log)
        self.assertGreater(len([ln for ln in log if ln.startswith("gh pr view")]),
                           1, log)  # it polled durable state
        self.assertFalse(any("issue edit 8" in ln for ln in log), log)

    def test_the_tick_still_holds_its_lock_while_it_waits_for_review(self):
        # AC (#54): waiting happens inside the tick, so an overlapping tick
        # cannot start work on this superproject meanwhile.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk")])
        h.set_view_story(story(7, "in-review", "afk"))
        self.reviewed_pull_request(h)
        proc = h.run(lock_probe=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertIn("tick-lock held", log)
        self.assertNotIn("tick-lock free", log)

    def test_the_expired_window_hands_off_without_moving_the_reviewed_head(self):
        # AC (#54): expiry writes a Handoff and ends the tick cleanly. It must
        # be comment-only -- a commit would move the head the review is bound to.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk")])
        h.set_view_story(story(7, "in-review", "afk"))
        self.reviewed_pull_request(h)
        proc = h.run()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        handoff = [ln for ln in log
                   if ln.startswith("gh issue comment 7") and "ralph:handoff" in ln]
        self.assertEqual(len(handoff), 1, log)
        self.assertFalse(any(ln.startswith("git commit") or ln.startswith("git push")
                             for ln in log), log)
        self.assertIn("window", proc.stdout)
        # The Story stays In Review, which is what makes the next tick resume it
        # (and rediscover its pull request) ahead of any state:ready work.
        self.assertFalse(any("issue edit 7" in ln for ln in log), log)

    def deadlocked_story(self, number=7, head="a" * 40):
        """A Story whose one round requested changes and was answered."""
        record = json.dumps({
            "contract": "ralph-review/v1", "verdict": "request_changes",
            "head": head, "model": "gpt-5-codex", "round": 1,
            "summary": "One blocker.",
            "findings": [{"id": "F-1", "blocking": True,
                          "category": "missing_tests",
                          "claim": "The guard is untested.",
                          "evidence": "no fixture over the limit",
                          "requirement": "acceptance criterion 2",
                          "verification": "add one"}]}, indent=2)
        answer = json.dumps({
            "contract": "ralph-response/v1", "head": head, "round": 1,
            "model": "claude-opus-5", "summary": "Disputed.",
            "dispositions": [{"id": "F-1", "disposition": "disputed",
                              "note": "Already covered.",
                              "evidence": "test_x.py:210"}]}, indent=2)
        return dict(story(number, "in-review", "afk"), comments=[
            {"body": "<!-- ralph-review-result:v1 head=%s round=1 -->\n\n"
                     "```json\n%s\n```" % (head, record)},
            {"body": "<!-- ralph-review-response:v1 head=%s round=1 -->\n\n"
                     "```json\n%s\n```" % (head, answer)}])

    def test_a_deadlocked_story_is_blocked_and_the_tick_works_on(self):
        # AC (#57): the round budget runs out with the models still disagreeing.
        # That Story alone stops -- blocked, with a human asked to arbitrate --
        # and the tick goes straight on to unrelated ready work.
        h = self.harness()
        h.set_review_window(0.005, 0.01, max_rounds=1)
        blocked = dict(story(7, "blocked", "afk"))
        h.set_backlogs([self.deadlocked_story(), story(8, "ready", "afk")],
                       [blocked, story(8, "ready", "afk")],   # circuit breaker
                       [blocked, story(8, "ready", "afk")],   # next selection
                       [], [])
        h.set_view_stories(self.deadlocked_story(), story(8, "ready", "afk"))
        self.reviewed_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("pr edit 70 --add-reviewer" in ln for ln in log), log)
        self.assertTrue(any("issue edit 7" in ln and "state:blocked" in ln
                            for ln in log), log)
        # Unrelated work carried on inside the same tick.
        self.assertTrue(any("issue edit 8" in ln and "state:in-progress" in ln
                            for ln in log), log)
        launched = h.agent_calls()
        self.assertEqual(len(launched), 1, log)
        self.assertIn("issue=8", launched[0])

    def test_a_deadlock_alone_never_halts_the_loop(self):
        # AC (#57): this is not the global halt. Whether the loop stops stays
        # the circuit breaker's decision, made from how many Stories are blocked.
        h = self.harness()
        h.set_review_window(0.005, 0.01, max_rounds=1)
        blocked = dict(story(7, "blocked", "afk"))
        h.set_backlogs([self.deadlocked_story()], [blocked], [], [], [])
        h.set_view_stories(self.deadlocked_story())
        self.reviewed_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("needs-human" in ln for ln in log), log)
        # But the breaker was consulted: one more blocked Story and it halts.
        self.assertIn("deadlocked", proc.stdout)

    def test_enough_deadlocks_still_halt_the_whole_loop(self):
        # AC (#57): the blocked Story feeds the existing circuit-breaker count,
        # which retains authority over the global halt.
        h = self.harness()
        h.set_review_window(0.005, 0.01, max_rounds=1)
        # full.yml sets circuit_breaker: 3, so two Stories were already blocked
        # before this deadlock made it three.
        earlier = [dict(story(n, "blocked", "afk")) for n in (5, 6)]
        blocked = dict(story(7, "blocked", "afk"))
        h.set_backlogs(earlier + [self.deadlocked_story()],
                       earlier + [blocked],   # circuit breaker: three blocked
                       [], [], [])
        h.set_view_stories(self.deadlocked_story())
        self.reviewed_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("needs-human" in ln for ln in log), log)

    def arbitrated_pull_request(self, h, issue=7, head="a" * 40, review=None):
        """A marked, reviewed pull request plus whatever the human did to it."""
        with open(os.path.join(h.queue, "head.txt"), "w") as fh:
            fh.write(head + "\n")
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #%d\n" % issue
        reviews = [{"body": "<!-- ralph-review:v1 head=%s -->" % head,
                    "state": "COMMENTED", "id": "R-0",
                    "author": {"login": "ralph"}}]
        if review is not None:
            reviews.append(review)
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": head, "baseRefOid": "b" * 40, "comments": [],
             "reviews": reviews})

    def test_a_human_approval_releases_the_gate_on_a_blocked_story(self):
        # AC (#58): Approve is authoritative -- it releases the model-review
        # gate over unresolved findings and clears the escalation.
        h = self.harness()
        blocked = dict(story(7, "blocked", "afk"))
        # dry-run (no-work: a blocked Story is never selected), then the
        # completion pass, then the arbitration pass's own scan.
        h.set_backlogs([blocked], [], [blocked])
        h.set_view_stories(blocked)
        self.arbitrated_pull_request(h, review={
            "id": "R-1", "state": "APPROVED", "body": "Ship it.",
            "author": {"login": "carl"}})

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        status = [ln for ln in log if "statuses/" + "a" * 40 in ln]
        self.assertTrue(status, log)
        self.assertIn("state=success", status[0])
        self.assertTrue(any("issue edit 7" in ln and "state:in-review" in ln
                            and "state:blocked" in ln for ln in log), log)
        self.assertTrue(any("ralph-human-arbitration:v1" in ln for ln in log), log)

    def test_an_ordinary_human_comment_changes_no_label_check_or_state(self):
        # AC (#58): comments enrich the record and decide nothing.
        h = self.harness()
        blocked = dict(story(7, "blocked", "afk"))
        # dry-run (no-work: a blocked Story is never selected), then the
        # completion pass, then the arbitration pass's own scan.
        h.set_backlogs([blocked], [], [blocked])
        h.set_view_stories(blocked)
        self.arbitrated_pull_request(h, review={
            "id": "R-1", "state": "COMMENTED", "body": "Reads well.",
            "author": {"login": "carl"}})

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("issue edit 7" in ln for ln in log), log)
        self.assertFalse(any("statuses/" in ln for ln in log), log)
        self.assertFalse(any("ralph-human-arbitration:v1" in ln for ln in log), log)
        self.assertEqual(h.agent_calls(), [], log)

    def test_requested_changes_reopens_the_story_and_launches_the_model(self):
        # AC (#58): authoritative feedback goes back to the implementation
        # model, with the human's own words.
        h = self.harness()
        blocked = dict(story(7, "blocked", "afk"))
        # dry-run (no-work: a blocked Story is never selected), then the
        # completion pass, then the arbitration pass's own scan.
        h.set_backlogs([blocked], [], [blocked])
        h.set_view_stories(blocked)
        self.arbitrated_pull_request(h, review={
            "id": "R-1", "state": "CHANGES_REQUESTED",
            "body": "Move the guard into the caller.",
            "author": {"login": "carl"}})

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("issue edit 7" in ln and "state:in-review" in ln
                            for ln in log), log)
        self.assertEqual(len(h.agent_calls()), 1, log)
        # No check was released: only an Approve does that.
        self.assertFalse(any("statuses/" in ln for ln in log), log)

    def approved_pull_request(self, h, issue=7, head="a" * 40, ci="SUCCESS"):
        """A marked pull request whose head carries a satisfied review gate."""
        with open(os.path.join(h.queue, "head.txt"), "w") as fh:
            fh.write(head + "\n")
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #%d\n" % issue
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": head, "baseRefOid": "b" * 40, "comments": [],
             "reviews": [{"body": "<!-- ralph-review:v1 head=%s -->" % head,
                          "state": "COMMENTED", "id": "R-0",
                          "author": {"login": "ralph"}}],
             "statusCheckRollup": [
                 {"name": "test", "status": "COMPLETED", "conclusion": ci},
                 {"context": "ralph/model-review", "state": "SUCCESS"}]})

    def test_an_approved_afk_story_merges_and_closes_as_passing(self):
        # AC (#59): the whole AFK flow, end to end against fake binaries.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk")], [], [], [])
        h.set_view_stories(story(7, "in-review", "afk"))
        self.approved_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        merge = [ln for ln in log if "pr merge 70" in ln]
        self.assertTrue(merge, log)
        self.assertIn("--squash", merge[0])
        self.assertTrue(any("issue close 7" in ln for ln in log), log)
        self.assertEqual(h.agent_calls(), [], log)
        # It never targets main.
        self.assertFalse(any("main" in ln for ln in log), log)

    def test_an_approved_hil_story_parks_at_awaiting_bench_unmerged(self):
        # AC (#59): the whole HIL flow -- same approvals, no merge, still open.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "hil")], [], [], [])
        h.set_view_stories(story(7, "in-review", "hil"))
        self.approved_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("issue edit 7" in ln and "state:awaiting-bench" in ln
                            for ln in log), log)
        self.assertFalse(any("pr merge" in ln for ln in log), log)
        self.assertFalse(any("issue close" in ln for ln in log), log)

    def test_red_ci_completes_nothing_however_approving_the_review(self):
        # AC (#59): it merges only when *both* halves hold for this head.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([story(7, "in-review", "afk")], [], [], [])
        h.set_view_stories(story(7, "in-review", "afk"))
        self.approved_pull_request(h, ci="FAILURE")

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("pr merge" in ln for ln in log), log)
        self.assertFalse(any("issue close" in ln for ln in log), log)

    def test_a_protected_change_asks_a_human_instead_of_merging(self):
        # AC (#60): a change to Ralph's own control plane is never completed on
        # a model review alone -- the mechanism cannot approve changes to
        # itself -- and the pull request says why.
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        with open(os.path.join(h.tmp, ".ralph.yml"), "a") as fh:
            fh.write("control_plane:\n  protected:\n    - prompts/**\n")
        with open(os.path.join(h.queue, "changed.txt"), "w") as fh:
            fh.write("prompts/review.v1.md\nlib/thing.py\n")
        h.set_backlogs([story(7, "in-review", "afk")], [], [], [])
        h.set_view_stories(story(7, "in-review", "afk"))
        self.approved_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("pr merge" in ln for ln in log), log)
        self.assertFalse(any("issue close" in ln for ln in log), log)
        notice = [ln for ln in log if ln.startswith("gh pr comment 70")]
        self.assertTrue(notice, log)
        self.assertTrue(any("prompts/review.v1.md" in ln for ln in log), log)
        self.assertTrue(any("--add-reviewer" in ln for ln in log), log)
        # Nothing is blocked: the negotiation is over, not broken.
        self.assertFalse(any("state:blocked" in ln for ln in log), log)

    def test_a_human_approval_lets_a_protected_change_complete(self):
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        with open(os.path.join(h.tmp, ".ralph.yml"), "a") as fh:
            fh.write("control_plane:\n  protected:\n    - prompts/**\n")
        with open(os.path.join(h.queue, "changed.txt"), "w") as fh:
            fh.write("prompts/review.v1.md\n")
        approved = dict(story(7, "in-review", "afk"), comments=[
            {"body": "<!-- ralph-human-arbitration:v1 review=R-1 -->\n\n"
                     "```json\n" + json.dumps(
                         {"review": "R-1", "decision": "APPROVED",
                          "reviewer": "carl", "head": "a" * 40,
                          "overrode": []}, indent=2) + "\n```"}])
        h.set_backlogs([story(7, "in-review", "afk")], [], [], [])
        h.set_view_stories(approved)
        self.approved_pull_request(h)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("pr merge 70" in ln for ln in log), log)

    def test_green_afk_story_opens_marked_pr_and_enters_review(self):
        h = self.harness()
        h.set_backlogs([story(7, "ready", "afk")], [])  # then no-work -> stop
        h.set_view_story(story(7, "in-progress", "afk"))
        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        agent_calls = h.agent_calls()
        self.assertEqual(len(agent_calls), 1, log)  # promoted, not re-run
        self.assertTrue(any("pr create" in ln and "ralph-managed-pr:v1" in ln
                            for ln in log), log)
        self.assertTrue(any("state:in-review" in ln for ln in log), log)
        self.assertFalse(any("pr merge" in ln or "issue close" in ln for ln in log), log)

    def test_green_hil_story_opens_marked_pr_to_in_review(self):
        h = self.harness()
        h.set_backlogs([story(5, "in-progress", "hil")], [])
        h.set_view_story(story(5, "in-progress", "hil"))
        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("pr create" in ln for ln in log), log)
        self.assertTrue(any("state:in-review" in ln for ln in log), log)
        self.assertFalse(any("state:awaiting-bench" in ln for ln in log), log)
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


class TheStoryIsTheUnitOfThePullRequest(unittest.TestCase):
    """PRD #69, end to end at the tick, entirely offline.

    Two successive Stories of one Feature each open their own marked pull
    request against the Feature integration branch; the first merges into it;
    the second is based on the branch its predecessor merged into, and its
    first Negotiation Round is round one. The Orphan Story path runs beside it
    unchanged, as this Feature's regression boundary.
    """

    def harness(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return TickHarness(tmp)

    def test_no_provider_override_survives_into_the_tick(self):
        """Offline by construction: only the fakes on PATH can be launched."""
        env = self.harness().env()
        for adapter in ralph_agent.PROVIDERS.values():
            self.assertNotIn(adapter.binary_env, env)

    # -- the Feature's first Story ------------------------------------------

    def test_the_first_story_creates_the_feature_branch_and_its_own_pr(self):
        h = self.harness()
        h.set_backlogs([prd_issue(), feature_story(20, "ready")], [], [], [])
        h.set_view_stories(prd_issue(), feature_story(20, "in-progress"))
        h.set_remote_branches()  # the Feature has not started yet

        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertTrue(any("git push origin origin/develop:refs/heads/%s"
                            % FEATURE_BRANCH in ln for ln in log), log)
        self.assertTrue(any("git push -u origin HEAD:ralph/20-story-20" in ln
                            for ln in log), log)
        create = next(ln for ln in log if "pr create" in ln)
        self.assertIn("--base %s" % FEATURE_BRANCH, create)
        self.assertIn("--head ralph/20-story-20", create)
        self.assertIn("ralph-managed-pr:v1", create)
        self.assertFalse(any("pr merge" in ln or "issue close" in ln
                             for ln in log), log)

    def test_the_feature_branch_is_not_recreated_once_it_exists(self):
        h = self.harness()
        h.set_backlogs([prd_issue(), feature_story(20, "ready")], [], [], [])
        h.set_view_stories(prd_issue(), feature_story(20, "in-progress"))
        h.set_remote_branches(FEATURE_BRANCH)

        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("refs/heads/%s" % FEATURE_BRANCH in ln
                             for ln in log), log)
        self.assertTrue(any("--base %s" % FEATURE_BRANCH in ln for ln in log), log)

    def approved(self, h, issue, head="a" * 40):
        with open(os.path.join(h.queue, "head.txt"), "w") as fh:
            fh.write(head + "\n")
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #%d\n" % issue
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": head, "baseRefOid": "b" * 40, "comments": [],
             "reviews": [{"body": "<!-- ralph-review:v1 head=%s -->" % head,
                          "state": "COMMENTED", "id": "R-0",
                          "author": {"login": "ralph"}}],
             "statusCheckRollup": [
                 {"name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
                 {"context": "ralph/model-review", "state": "SUCCESS"}]})

    def test_the_first_story_merges_into_the_feature_branch_and_closes(self):
        h = self.harness()
        h.set_review_window(0.005, 0.01)
        h.set_backlogs([prd_issue(), feature_story(20, "in-review")],
                       [], [], [])
        h.set_view_stories(prd_issue(), feature_story(20, "in-review"))
        h.set_remote_branches(FEATURE_BRANCH)
        self.approved(h, 20)

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        merge = next(ln for ln in log if "pr merge 70" in ln)
        self.assertIn("--squash", merge)
        self.assertIn("--delete-branch", merge)
        self.assertTrue(any("issue close 20" in ln for ln in log), log)

    # -- the Feature's second Story -----------------------------------------

    def test_the_second_story_opens_its_own_pr_on_the_feature_branch(self):
        """Its base is the branch its predecessor merged into: the current tip."""
        h = self.harness()
        h.set_backlogs([prd_issue(),
                        dict(feature_story(20, "in-review"), state="CLOSED"),
                        feature_story(21, "ready")], [], [], [])
        h.set_view_stories(prd_issue(), feature_story(21, "in-progress"))
        h.set_remote_branches(FEATURE_BRANCH)

        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        create = next(ln for ln in log if "pr create" in ln)
        self.assertIn("--base %s" % FEATURE_BRANCH, create)
        self.assertIn("--head ralph/21-story-21", create)
        self.assertNotIn("ralph/20-story-20", create)
        self.assertTrue(any("Refs #21" in ln for ln in log), log)
        # The Feature branch already carries its predecessor, so nothing
        # recreates it off the base branch under the second Story's feet.
        self.assertFalse(any("origin/develop:refs/heads/" in ln for ln in log), log)

    def test_the_second_storys_first_round_is_round_one(self):
        """The bug PRD #69 was written for, reproduced at the tick.

        Under the shared pull request the round budget was the *Feature's*: a
        later Story inherited every round its predecessors spent and escalated
        to a human at the limit having never been reviewed. Here the pull
        request carries two earlier Stories' review stamps and the budget is
        one -- and the Story, which has spent nothing, is still reviewed.
        """
        h = self.harness()
        h.set_review_window(0.005, 0.01, max_rounds=1)
        h.set_backlogs([prd_issue(), feature_story(21, "in-review")],
                       [], [], [], [])
        h.set_view_stories(prd_issue(), feature_story(21, "in-review"))
        h.set_remote_branches(FEATURE_BRANCH)
        head = "c" * 40
        with open(os.path.join(h.queue, "head.txt"), "w") as fh:
            fh.write(head + "\n")
        body = "<!-- ralph-managed-pr:v1 -->\n\nRefs #21\n"
        h.set_pull_requests(
            [{"number": 70, "body": body}],
            {"number": 70, "body": body, "state": "OPEN",
             "headRefOid": head, "baseRefOid": "b" * 40, "comments": [],
             "reviews": [
                 {"body": "<!-- ralph-review:v1 head=%s -->" % ("a" * 40)},
                 {"body": "<!-- ralph-review:v1 head=%s -->" % ("b" * 40)}]})

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("state:blocked" in ln for ln in log), log)
        self.assertFalse(any("--add-reviewer" in ln for ln in log), log)
        self.assertTrue(h.agent_calls(), log)  # it reviewed, round one

    # -- the Feature boundary ------------------------------------------------

    def test_a_feature_is_not_integrated_while_a_hil_story_is_open(self):
        """Two gates agree, and the tick reaches the outer one first.

        A Feature is only *eligible* for the pass once every one of its Stories
        is closed, so an open HIL Story stops it before the pass is even
        invoked. The pass's own refusal -- which names each unverified Story --
        is the net under that, exercised in test_feature_complete.py.
        """
        h = self.harness()
        closed = dict(feature_story(20, "in-review"), state="CLOSED")
        open_hil = feature_story(21, "awaiting-bench", type_="hil")
        backlog = [prd_issue(), closed, open_hil]
        h.set_backlogs([], backlog, backlog, [])
        h.set_view_stories(prd_issue())
        h.mock_make()

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        self.assertFalse(any("pr merge" in ln for ln in log), log)
        self.assertFalse(any("issue close 42" in ln for ln in log), log)
        self.assertNotIn("completion pass", proc.stdout)

    def test_a_feature_integrates_as_one_merge_once_its_hil_story_closes(self):
        h = self.harness()
        verified = dict(feature_story(21, "awaiting-bench", type_="hil"),
                        state="CLOSED")
        backlog = [prd_issue(),
                   dict(feature_story(20, "in-review"), state="CLOSED"),
                   verified]
        h.set_backlogs([], backlog, backlog, [])
        h.set_view_stories(prd_issue())
        h.mock_make()

        proc = h.run()

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        create = next(ln for ln in log if "pr create" in ln)
        self.assertIn("--base develop", create)
        self.assertIn("--head %s" % FEATURE_BRANCH, create)
        merge = next(ln for ln in log if "pr merge" in ln)
        self.assertIn("--merge", merge)
        self.assertTrue(any("issue close 42" in ln for ln in log), log)

    # -- the regression boundary ---------------------------------------------

    def test_an_orphan_story_runs_through_the_same_tick_unchanged(self):
        h = self.harness()
        h.set_backlogs([story(7, "ready", "afk")], [], [], [])
        h.set_view_story(story(7, "in-progress", "afk"))

        proc = h.run(agent_emit=STORY_COMPLETE_MARKER)

        self.assertEqual(proc.returncode, 0, proc.stdout)
        log = h.log_lines()
        create = next(ln for ln in log if "pr create" in ln)
        self.assertIn("--base develop", create)
        self.assertIn("--head ralph/7-story-7", create)
        # No Feature, so origin is never asked about one and none is created.
        self.assertFalse(any("ls-remote" in ln for ln in log), log)
        self.assertFalse(any("refs/heads/feature/" in ln for ln in log), log)


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
                h.set_view_story(story(7, "in-progress", "afk"))
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

    def test_a_done_signal_enters_review_whichever_adapter_ran(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                h = self.harness(provider)
                h.set_backlogs([story(7, "ready", "afk")], [])
                h.set_view_story(story(7, "in-progress", "afk"))
                proc = h.run(agent_emit=STORY_COMPLETE_MARKER)
                self.assertEqual(proc.returncode, 0, proc.stdout)
                log = h.log_lines()
                self.assertEqual(len(h.agent_calls(provider)), 1, log)  # not re-run
                self.assertTrue(any("pr create" in ln for ln in log), log)
                self.assertTrue(any("state:in-review" in ln for ln in log), log)
                self.assertFalse(any("pr merge" in ln or "issue close" in ln
                                     for ln in log), log)

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
        # No Handoff and no Attempt: the story is simply left where it is. The
        # token ledger (#63) is not either of those -- it records that the
        # invocation happened, which it did.
        self.assertFalse(any(ralph_handoff.HANDOFF_MARKER in ln for ln in log), log)
        self.assertFalse(any(ralph_failure.ATTEMPT_MARKER in ln for ln in log), log)


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
