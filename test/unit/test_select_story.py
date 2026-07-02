"""Unit tests for the pure story-selection engine (US-004, ADR-0002).

Selection is a pure function over a normalized backlog: it decides the next
action Ralph should take (resume #N / start #N / no-work / halt) and changes
nothing. Stories are fed in `gh issue view --json` shape and normalized before
selection, exactly as `ralph --dry-run` will feed the live backlog.
"""
import json
import os
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB_DIR = os.path.join(REPO_ROOT, "lib")
BACKLOGS = os.path.join(REPO_ROOT, "test", "fixtures", "backlogs")
RALPH = os.path.join(REPO_ROOT, "bin", "ralph")

sys.path.insert(0, LIB_DIR)
import ralph_select  # noqa: E402


def story(number, state=None, type_=None, prio=1, depends=None, closed=False,
          blocker=False, needs_human=False, bench=False):
    """Build a story in `gh issue --json number,title,labels,body,state` shape."""
    labels = []
    if state:
        labels.append("state:" + state)
    if type_:
        labels.append("type:" + type_)
    if prio is not None:
        labels.append("prio:%d" % prio)
    if blocker:
        labels.append("ready-for-human")
    if needs_human:
        labels.append("needs-human")
    body = "## Acceptance Criteria\n- [ ] do the thing\n"
    if bench:
        body += "\n## Bench Test Procedure\n1. flash and observe\n"
    dep_str = ", ".join("#%d" % d for d in depends) if depends else "None"
    body += "\nDepends on: %s\n" % dep_str
    return {
        "number": number,
        "title": "S%d" % number,
        "labels": [{"name": n} for n in labels],
        "body": body,
        "state": "CLOSED" if closed else "OPEN",
    }


def action_for(*stories):
    return ralph_select.next_action(list(stories))


class ResumeFirst(unittest.TestCase):
    def test_in_progress_is_chosen_before_any_ready_scan(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=1),
            story(2, state="in-progress", type_="afk", prio=9),
        )
        self.assertEqual(act.kind, ralph_select.RESUME)
        self.assertEqual(act.number, 2)

    def test_lowest_prio_then_number_among_in_progress(self):
        act = action_for(
            story(5, state="in-progress", type_="afk", prio=2),
            story(2, state="in-progress", type_="afk", prio=1),
            story(3, state="in-progress", type_="afk", prio=1),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.RESUME, 2))


class Ordering(unittest.TestCase):
    def test_lower_prio_number_wins(self):
        act = action_for(
            story(3, state="ready", type_="afk", prio=2),
            story(1, state="ready", type_="afk", prio=1),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 1))

    def test_tie_breaks_on_lowest_issue_number_fifo(self):
        act = action_for(
            story(7, state="ready", type_="afk", prio=1),
            story(4, state="ready", type_="afk", prio=1),
        )
        self.assertEqual(act.number, 4)

    def test_blocked_state_is_skipped(self):
        act = action_for(
            story(1, state="blocked", type_="afk", prio=1),
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_design_decision_blocker_is_skipped(self):
        act = action_for(
            story(1, blocker=True, prio=1),  # ready-for-human, no state:ready
            story(2, state="ready", type_="afk", prio=2),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))


class Dependencies(unittest.TestCase):
    def test_afk_dependency_still_open_blocks_selection(self):
        # #2 has the better prio but depends on an unmerged (open) AFK story.
        act = action_for(
            story(1, state="ready", type_="afk", prio=9),
            story(2, state="ready", type_="afk", prio=1, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 1))

    def test_afk_dependency_merged_closed_is_satisfied(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=9, closed=True),
            story(2, state="ready", type_="afk", prio=1, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_hil_dependency_awaiting_bench_is_not_satisfied(self):
        # A HIL dep is satisfied only once bench-verified (closed); awaiting-bench
        # is still open, so the dependent is never selected.
        act = action_for(
            story(1, state="awaiting-bench", type_="hil", prio=1, bench=True),
            story(2, state="ready", type_="afk", prio=2, depends=[1]),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_hil_dependency_bench_verified_closed_is_satisfied(self):
        act = action_for(
            story(1, state="awaiting-bench", type_="hil", prio=1, bench=True, closed=True),
            story(2, state="ready", type_="afk", prio=2, depends=[1]),
        )
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))

    def test_dependency_absent_from_backlog_is_treated_as_done(self):
        act = action_for(story(2, state="ready", type_="afk", prio=1, depends=[99]))
        self.assertEqual((act.kind, act.number), (ralph_select.START, 2))


class NoWorkAndHalt(unittest.TestCase):
    def test_empty_backlog_is_no_work(self):
        self.assertEqual(action_for().kind, ralph_select.NO_WORK)

    def test_all_blocked_backlog_is_no_work(self):
        act = action_for(
            story(1, state="blocked", type_="afk", prio=1),
            story(2, state="blocked", type_="hil", prio=2, bench=True),
        )
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_closed_stories_are_ignored(self):
        act = action_for(story(1, state="ready", type_="afk", prio=1, closed=True))
        self.assertEqual(action_for(story(1, state="ready", type_="afk", prio=1, closed=True)).kind,
                         ralph_select.NO_WORK)
        self.assertEqual(act.kind, ralph_select.NO_WORK)

    def test_needs_human_label_halts_the_loop(self):
        act = action_for(
            story(1, state="ready", type_="afk", prio=1),
            story(2, state="blocked", type_="afk", prio=2, needs_human=True),
        )
        self.assertEqual(act.kind, ralph_select.HALT)


class PureOverNormalizedList(unittest.TestCase):
    def test_select_next_operates_on_a_normalized_list(self):
        raw = [
            story(1, state="ready", type_="afk", prio=2),
            story(2, state="in-progress", type_="afk", prio=5),
        ]
        normalized = ralph_select.normalize(raw)
        # normalize is pure: fields the engine needs, no gh objects left.
        self.assertEqual(normalized[1]["state"], "in-progress")
        self.assertFalse(normalized[1]["closed"])
        act = ralph_select.select_next(normalized)
        self.assertEqual((act.kind, act.number), (ralph_select.RESUME, 2))


class CliDryRun(unittest.TestCase):
    def _run(self, path):
        return subprocess.run(
            [RALPH, "--dry-run", path],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_dry_run_prints_start_action_for_a_ready_backlog(self):
        proc = self._run(os.path.join(BACKLOGS, "ready.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().strip(), "start #10")

    def test_dry_run_prints_no_work_for_empty_backlog(self):
        proc = self._run(os.path.join(BACKLOGS, "empty.json"))
        self.assertEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertEqual(proc.stdout.decode().strip(), "no-work")


if __name__ == "__main__":
    unittest.main()
